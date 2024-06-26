# pylint: disable=too-many-locals,too-many-branches,too-many-statements
import enum
import logging as log
import time
from collections import namedtuple
from datetime import datetime, timedelta

from . import git
from .branch import Branch
from .interval import IntervalUnion
from .project import Project
from .user import User
from .pipeline import Pipeline


@enum.unique
class MergeStrategy(enum.Enum):
    merge = 'merge'
    rebase = 'rebase'
    rebase_then_merge = 'rebase_then_merge'

    # convince argparse to omit the MergeStrategy. prefix
    def __str__(self):
        return self.value


class MergeJob(object):

    def __init__(self, *, api, user, project, repo, options):
        self._api = api
        self._user = user
        self._project = project
        self._repo = repo
        self._options = options
        self._merge_timeout = timedelta(minutes=5)

    @property
    def repo(self):
        return self._repo

    @property
    def opts(self):
        return self._options

    def execute(self):
        raise NotImplementedError

    def ensure_mergeable_mr(self, merge_request):
        merge_request.refetch_info()
        log.info('Ensuring MR !%s is mergeable', merge_request.iid)
        log.debug('Ensuring MR %r is mergeable', merge_request)

        if merge_request.work_in_progress:
            raise CannotMerge("Sorry, I can't merge requests marked as Work-In-Progress!")

        if merge_request.squash and self._options.requests_commit_tagging:
            raise CannotMerge(
                "Sorry, merging requests marked as auto-squash would ruin my commit tagging!"
            )

        approvals = merge_request.fetch_approvals()
        if not approvals.sufficient:
            raise CannotMerge(
                'Insufficient approvals '
                '(have: {0.approver_usernames} missing: {0.approvals_left})'.format(approvals)
            )

        state = merge_request.state
        if state not in ('opened', 'reopened', 'locked'):
            if state in ('merged', 'closed'):
                raise SkipMerge('The merge request is already {}!'.format(state))
            else:
                raise CannotMerge('The merge request is in an unknown state: {}'.format(state))

        if self.during_merge_embargo():
            raise SkipMerge('Merge embargo!')

        if self._user.id not in merge_request.assignee_ids:
            raise SkipMerge('It is not assigned to me anymore!')

    def add_trailers(self, merge_request):

        log.info('Adding trailers for MR !%s', merge_request.iid)

        # add Reviewed-by
        reviewers = (
            _get_reviewer_names_and_emails(
                merge_request.fetch_approvals(),
                self._api,
            ) if self._options.add_reviewers else None
        )
        sha = None
        if reviewers is not None:
            sha = self._repo.tag_with_trailer(
                trailer_name='Reviewed-by',
                trailer_values=reviewers,
                branch=merge_request.source_branch,
                start_commit='origin/' + merge_request.target_branch,
            )

        # add Tested-by
        should_add_tested = self._options.add_tested and self._project.only_allow_merge_if_pipeline_succeeds
        tested_by = (
            ['{0._user.name} <{1.web_url}>'.format(self, merge_request)] if should_add_tested
            else None
        )
        if tested_by is not None and self._options.merge_strategy == MergeStrategy.rebase:
            sha = self._repo.tag_with_trailer(
                trailer_name='Tested-by',
                trailer_values=tested_by,
                branch=merge_request.source_branch,
                start_commit=merge_request.source_branch + '^'
            )

        # add Part-of
        part_of = (
            '<{0.web_url}>'.format(merge_request) if self._options.add_part_of
            else None
        )
        if part_of is not None:
            sha = self._repo.tag_with_trailer(
                trailer_name='Part-of',
                trailer_values=[part_of],
                branch=merge_request.source_branch,
                start_commit='origin/' + merge_request.target_branch,
            )
        return sha

    def get_mr_ci_status(self, merge_request, commit_sha=None, ci_run_by_me=False):
        if commit_sha is None:
            commit_sha = merge_request.sha
        pipelines = Pipeline.pipelines_by_branch(
            merge_request.source_project_id,
            merge_request.source_branch,
            self._api,
            username=self._user.username if ci_run_by_me else None,
        )
        current_pipeline = next(iter(pipeline for pipeline in pipelines if pipeline.sha == commit_sha), None)

        if current_pipeline:
            ci_status = current_pipeline.status
        else:
            log.warning('No pipeline listed for %s on branch %s', commit_sha, merge_request.source_branch)
            ci_status = None

        return ci_status

    def wait_for_ci_to_pass(self, merge_request, commit_sha=None):
        time_0 = datetime.utcnow()
        waiting_time_in_secs = 10

        if commit_sha is None:
            commit_sha = merge_request.sha

        ci_run_by_me = self._options.require_ci_run_by_me

        log.info('Waiting for CI to pass for MR !%s', merge_request.iid)
        while datetime.utcnow() - time_0 < self._options.ci_timeout:
            ci_status = self.get_mr_ci_status(
                merge_request, commit_sha=commit_sha, ci_run_by_me=ci_run_by_me)
            if ci_status == 'success':
                log.info('CI for MR !%s passed', merge_request.iid)
                return

            if ci_status == 'skipped':
                log.info('CI for MR !%s skipped', merge_request.iid)
                return

            if ci_status == 'failed':
                raise CannotMerge('CI failed!')

            if ci_status == 'canceled':
                raise CannotMerge('Someone canceled the CI.')

            if ci_status is None and ci_run_by_me:
                log.info('Starting a CI in my name')
                pipeline = Pipeline.start(
                    merge_request.source_project_id, merge_request.source_branch, self._api)
                log.info('Started pipeline %s', pipeline.id)
            elif ci_status not in ('pending', 'running', 'created'):
                log.warning('Suspicious CI status: %r', ci_status)

            log.debug('Waiting for %s secs before polling CI status again', waiting_time_in_secs)
            time.sleep(waiting_time_in_secs)

        if self._options.ci_timeout_skip:
            raise SkipMerge('CI is taking too long.')
        else:
            raise CannotMerge('CI is taking too long.')

    def unassign_from_mr(self, merge_request):
        log.info('Unassigning from MR !%s', merge_request.iid)
        author_id = merge_request.author_id
        if author_id != self._user.id:
            merge_request.assign_to(author_id)
        else:
            merge_request.unassign()

    def during_merge_embargo(self):
        now = datetime.utcnow()
        return self.opts.embargo.covers(now)

    def maybe_reapprove(self, merge_request, approvals):
        # Re-approve the merge request, in case us pushing it has removed approvals.
        if self.opts.reapprove:
            # approving is not idempotent, so we need to check first that there are no approvals,
            # otherwise we'll get a failure on trying to re-instate the previous approvals
            def sufficient_approvals():
                return merge_request.fetch_approvals().sufficient
            # Make sure we don't race by ensuring approvals have reset since the push
            time_0 = datetime.utcnow()
            waiting_time_in_secs = 5
            log.info('Checking if approvals have reset')
            while sufficient_approvals() and datetime.utcnow() - time_0 < self._options.approval_timeout:
                log.debug('Approvals haven\'t reset yet, sleeping for %s secs', waiting_time_in_secs)
                time.sleep(waiting_time_in_secs)
            if not sufficient_approvals():
                approvals.reapprove()

    def fetch_source_project(self, merge_request):
        remote = 'origin'
        remote_url = None
        source_project = self.get_source_project(merge_request)
        if source_project is not self._project:
            remote = 'source'
            remote_url = source_project.ssh_url_to_repo
            self._repo.fetch(
                remote_name=remote,
                remote_url=remote_url,
            )
        return source_project, remote_url, remote

    def get_source_project(self, merge_request):
        source_project = self._project
        if merge_request.source_project_id != self._project.id:
            source_project = Project.fetch_by_id(
                merge_request.source_project_id,
                api=self._api,
            )
        return source_project

    def fuse(self, source, target, source_repo_url=None, local=False):
        # NOTE: this leaves git switched to branch_a
        strategies = {
            MergeStrategy.merge: self._repo.merge,
            MergeStrategy.rebase: self._repo.rebase,
            MergeStrategy.rebase_then_merge: self.rebase_then_merge
        }
        strategy = strategies[self._options.merge_strategy]
        return strategy(
            source,
            target,
            source_repo_url=source_repo_url,
            local=local,
        )

    def rebase_then_merge(self, *args, **kw):
        try:
            return self._repo.rebase(*args, **kw)
        except git.GitError as rebase_error:
            log.info('rebase failed, trying merge: %s', rebase_error)
            try:
                return self._repo.merge(*args, **kw)
            except git.GitError as merge_error:
                log.info('merge also failed: %s', merge_error)
                raise
                # The caller will handle GitError

    def update_from_target_branch_and_push(
            self,
            merge_request,
            *,
            source_repo_url=None,
    ):
        """Updates `target_branch` with commits from `source_branch`, optionally add trailers and push.
        The update strategy can either be rebase or merge. The default is rebase.

        Returns
        -------
        (sha_of_target_branch, sha_after_update, sha_after_rewrite)
        """
        repo = self._repo
        source_branch = merge_request.source_branch
        target_branch = merge_request.target_branch
        assert source_repo_url != repo.remote_url
        if source_repo_url is None and source_branch == target_branch:
            raise CannotMerge('source and target branch seem to coincide!')

        branch_updated = branch_rewritten = changes_pushed = False
        try:
            updated_sha = self.fuse(
                source_branch,
                target_branch,
                source_repo_url=source_repo_url,
            )
            branch_updated = True
            # The fuse above fetches origin again, so we are now safe to fetch
            # the sha from the remote target branch.
            target_sha = repo.get_commit_hash('origin/' + target_branch)
            if updated_sha == target_sha:
                raise CannotMerge('these changes already exist in branch `{}`'.format(target_branch))
            rewritten_sha = self.add_trailers(merge_request) or updated_sha
            branch_rewritten = rewritten_sha != updated_sha
            repo.push(source_branch, source_repo_url=source_repo_url, force=True)
            changes_pushed = True
        except git.GitError as exc:
            log.info('GitError from fuse: {}'.format(exc))
            if not branch_updated:
                raise CannotMerge('got conflicts while rebasing, your problem now...')
            if not branch_rewritten:
                raise CannotMerge(
                    ('expected commit id to change on merge, but we have rewritten={}'
                     ' updated={}. Or it could be the GitError: {}')
                    .format(rewritten_sha, updated_sha, exc))
                # raise CannotMerge('failed on filter-branch; check my logs!')
            if not changes_pushed:
                if (
                    branch_rewritten and Branch.fetch_by_name(
                        merge_request.source_project_id, merge_request.source_branch, self._api,
                    ).protected
                ):
                    raise CannotMerge('Sorry, I can\'t push rewritten changes to protected branches!')
                raise CannotMerge(
                    'failed to push with strategy {}, check my logs!'.format(self.opts.merge_strategy.value))

            raise
        else:
            return target_sha, updated_sha, rewritten_sha
        finally:
            # A failure to clean up probably means something is fucked with the git repo
            # and likely explains any previous failure, so it will better to just
            # raise a GitError
            if source_branch != 'master':
                repo.checkout_branch('master')
                repo.remove_branch(source_branch)


def _get_reviewer_names_and_emails(approvals, api):
    """Return a list ['A. Prover <a.prover@example.com', ...]` for `merge_request.`"""

    uids = approvals.approver_ids
    return ['{0.name} <{0.email}>'.format(User.fetch_by_id(uid, api)) for uid in uids]


JOB_OPTIONS = [
    'add_tested',
    'add_part_of',
    'add_reviewers',
    'reapprove',
    'approval_timeout',
    'embargo',
    'ci_timeout',
    'merge_strategy',
    # bool - If true, require not just that a CI passed, but a CI run by marge.
    # Start one if necessary.
    'require_ci_run_by_me',
    'ci_timeout_skip',
]


class MergeJobOptions(namedtuple('MergeJobOptions', JOB_OPTIONS)):
    __slots__ = ()

    @property
    def requests_commit_tagging(self):
        return self.add_tested or self.add_part_of or self.add_reviewers

    @classmethod
    def default(
            cls, *,
            add_tested=False, add_part_of=False, add_reviewers=False, reapprove=False,
            approval_timeout=None, embargo=None, ci_timeout=None, ci_timeout_skip=False,
            merge_strategy=MergeStrategy.rebase,
            require_ci_run_by_me=False,
    ):
        approval_timeout = approval_timeout or timedelta(seconds=0)
        embargo = embargo or IntervalUnion.empty()
        ci_timeout = ci_timeout or timedelta(minutes=15)
        return cls(
            add_tested=add_tested,
            add_part_of=add_part_of,
            add_reviewers=add_reviewers,
            reapprove=reapprove,
            approval_timeout=approval_timeout,
            embargo=embargo,
            ci_timeout=ci_timeout,
            ci_timeout_skip=ci_timeout_skip,
            merge_strategy=merge_strategy,
            require_ci_run_by_me=require_ci_run_by_me,
        )


class CannotMerge(Exception):
    @property
    def reason(self):
        args = self.args
        if not args:
            return 'Unknown reason!'

        return args[0]


class SkipMerge(CannotMerge):
    pass
