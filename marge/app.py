"""
An auto-merger of merge requests for GitLab
"""

import contextlib
import logging
import logging.handlers
import os
import pwd
import re
import signal
import sys
import tempfile
import time
from datetime import timedelta

import configargparse

from . import bot
from . import error
from . import gitlab
from . import interval
from . import job
from . import user as user_module


class MargeBotCliArgError(Exception):
    pass


def time_interval(str_interval):
    try:
        quant, unit = re.match(r'\A([\d.]+) ?(h|m(?:in)?|s)?\Z', str_interval).groups()
        translate = {'h': 'hours', 'm': 'minutes', 'min': 'minutes', 's': 'seconds'}
        return timedelta(**{translate[unit or 's']: float(quant)})
    except (AttributeError, ValueError):
        raise configargparse.ArgumentTypeError('Invalid time interval (e.g. 12[s|min|h]): %s' % str_interval)


def _parse_config(args):

    def regexp(str_regex):
        try:
            return re.compile(str_regex)
        except re.error as err:
            raise configargparse.ArgumentTypeError('Invalid regexp: %r (%s)' % (str_regex, err.msg))

    parser = configargparse.ArgParser(
        auto_env_var_prefix='MARGE_',
        ignore_unknown_config_file_keys=True,  # Don't parse unknown args
        config_file_parser_class=configargparse.YAMLConfigFileParser,
        formatter_class=configargparse.ArgumentDefaultsRawHelpFormatter,
        description=__doc__,
    )
    # Log this on startup, so we can tell which version is running.
    parser.add_argument('--set-version', type=str)
    parser.add_argument('--log-name', type=str)
    parser.add_argument(
        '--config-file',
        env_var='MARGE_CONFIG_FILE',
        type=str,
        is_config_file=True,
        help='config file path',
    )
    auth_token_group = parser.add_mutually_exclusive_group(required=True)
    auth_token_group.add_argument(
        '--auth-token',
        type=str,
        metavar='TOKEN',
        help=(
            'Your GitLab token.\n'
            'DISABLED because passing credentials on the command line is insecure:\n'
            'You can still set it via ENV variable or config file, or use "--auth-token-file" flag.\n'
        ),
    )
    auth_token_group.add_argument(
        '--auth-token-file',
        type=configargparse.FileType('rt'),
        metavar='FILE',
        help='Path to your GitLab token file.\n',
    )
    parser.add_argument(
        '--gitlab-url',
        type=str,
        required=True,
        metavar='URL',
        help='Your GitLab instance, e.g. "https://gitlab.example.com".\n',
    )
    ssh_key_group = parser.add_mutually_exclusive_group(required=True)
    ssh_key_group.add_argument(
        '--ssh-key',
        type=str,
        metavar='KEY',
        help=(
            'The private ssh key for marge so it can clone/push.\n'
            'DISABLED because passing credentials on the command line is insecure:\n'
            'You can still set it via ENV variable or config file, or use "--ssh-key-file" flag.\n'
        ),
    )
    ssh_key_group.add_argument(
        '--ssh-key-file',
        type=str,  # because we want a file location, not the content
        metavar='FILE',
        help='Path to the private ssh key for marge so it can clone/push.\n',
    )
    parser.add_argument(
        '--embargo',
        type=interval.IntervalUnion.from_human,
        metavar='INTERVAL[,..]',
        help='Time(s) during which no merging is to take place, e.g. "Friday 1pm - Monday 9am".\n',
    )
    merge_strategy_group = parser.add_mutually_exclusive_group(required=False)
    merge_strategy_group.add_argument(
        '--use-merge-strategy',
        action='store_true',
        help=(
            'Use git merge instead of git rebase to update the *source* branch (EXPERIMENTAL)\n'
            'If you need to use a strict no-rebase workflow (in most cases\n'
            'you don\'t want this, even if you configured gitlab to use merge requests\n'
            'to use merge commits on the *target* branch (the default).)\n'
        ),
    )
    merge_strategy_group.add_argument(
        '--merge-strategy',
        type=job.MergeStrategy,
        default=job.MergeStrategy.rebase,
        choices=list(job.MergeStrategy),
        help=(
            'How to go about the merge. Exclusive with --use-merge-strategy, which is\n'
            'equivalent to --merge-strategy=merge.\n'
        ),
    )
    parser.add_argument(
        '--add-tested',
        action='store_true',
        help='Add "Tested: marge-bot <$MR_URL>" for the final commit on branch after it passed CI.\n',
    )
    parser.add_argument(
        '--batch',
        action='store_true',
        help='Enable processing MRs in batches\n',
    )
    parser.add_argument(
        '--add-part-of',
        action='store_true',
        help='Add "Part-of: <$MR_URL>" to each commit in MR.\n',
    )
    parser.add_argument(
        '--add-reviewers',
        action='store_true',
        help='Add "Reviewed-by: $approver" for each approver of MR to each commit in MR.\n',
    )
    parser.add_argument(
        '--impersonate-approvers',
        action='store_true',
        help='Marge-bot pushes effectively don\'t change approval status.\n',
    )
    parser.add_argument(
        '--approval-reset-timeout',
        type=time_interval,
        default='0s',
        help=(
            'How long to wait for approvals to reset after pushing.\n'
            'Only useful with the "new commits remove all approvals" option in a project\'s settings.\n'
            'This is to handle the potential race condition where approvals don\'t reset in GitLab\n'
            'after a force push due to slow processing of the event.\n'
        ),
    )
    parser.add_argument(
        '--project-regexp',
        type=regexp,
        default='.*',
        help="Only process projects that match; e.g. 'some_group/.*' or '(?!exclude/me)'.\n",
    )
    parser.add_argument(
        '--ci-timeout',
        type=time_interval,
        default='15min',
        help='How long to wait for CI to pass.\n',
    )
    parser.add_argument(
        '--max-ci-time-in-minutes',
        type=int,
        default=None,
        help='Deprecated; use --ci-timeout.\n',
    )
    parser.add_argument(
        '--require-ci-run-by-me',
        action='store_true',
        help=(
            'Require a successful CI started by me. Start one if necessary.\n'
            'The idea is that you can use $GITLAB_USER_LOGIN = marge-bot to run expensive merge-only CI.\n'
        ),
    )
    parser.add_argument(
        '--git-timeout',
        type=time_interval,
        default='120s',
        help='How long a single git operation can take.\n'
    )
    parser.add_argument(
        '--git-reference-repo',
        type=str,
        default=None,
        help='A reference repo to be used when git cloning.\n'
    )
    parser.add_argument(
        '--branch-regexp',
        type=regexp,
        default='.*',
        help='Only process MRs whose target branches match the given regular expression.\n',
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Debug logging (includes all HTTP requests etc).\n',
    )
    parser.add_argument(
        '--ci-timeout-skip',
        action='store_true',
        help='Skip to next MR if CI timeout expires (otherwise, give up on MR)'
    )
    parser.add_argument(
        '--skip-pending',
        action='store_true',
        help='Skip to next MR if oldest MR is not ready (otherwise, wait until it is)'
    )
    parser.add_argument(
        '--priority-labels',
        default='',
        help='Comma-separated labels, if all are present promote an MR to the front of the queue.',
    )
    config = parser.parse_args(args)

    if config.use_merge_strategy:
        config.merge_strategy = job.MergeStrategy.merge
    if config.merge_strategy == job.MergeStrategy.merge:
        if config.batch:
            raise MargeBotCliArgError('--merge-strategy=merge and --batch are currently mutually exclusive')
        elif config.add_tested:
            raise MargeBotCliArgError(
                '--merge-strategy=merge and --add-tested are currently mutually exclusive')
    del config.use_merge_strategy

    cli_args = []
    # pylint: disable=protected-access
    for _, (_, value) in parser._source_to_settings.get(configargparse._COMMAND_LINE_SOURCE_KEY, {}).items():
        cli_args.extend(value)
    for bad_arg in ['--auth-token', '--ssh-key']:
        if bad_arg in cli_args:
            raise MargeBotCliArgError('"%s" can only be set via ENV var or config file.' % bad_arg)
    return config


@contextlib.contextmanager
def _secret_auth_token_and_ssh_key(options):
    auth_token = options.auth_token or options.auth_token_file.readline().strip()
    if options.ssh_key_file:
        yield auth_token, options.ssh_key_file
    else:
        with tempfile.NamedTemporaryFile(mode='w', prefix='ssh-key-') as tmp_ssh_key_file:
            try:
                tmp_ssh_key_file.write(options.ssh_key + '\n')
                tmp_ssh_key_file.flush()
                yield auth_token, tmp_ssh_key_file.name
            finally:
                tmp_ssh_key_file.close()


def setup_logging(app_name, version):
    """Setup logging such that google-fluentd can parse it.

        This should be the standard setup for python logging at groq.
        Since we have no libraries yet, just copy paste it :/
    """
    # Seriously, this is how you log in UTC.
    logging.Formatter.converter = time.gmtime
    # For marge, the user will always be the same, but I must stick to the
    # convention so google-fluentd matches it properly.
    me = pwd.getpwuid(os.geteuid()).pw_name
    handler = logging.handlers.RotatingFileHandler(
        '/var/log/groq/%s.%s.pylog' % (me, app_name),
        'log/%s.marge.pylog' % (me,),
        maxBytes=4*1024*1024,
        backupCount=4,
    )
    handler.setFormatter(logging.Formatter(
        fmt='%(asctime)s:%(levelname)s:%(filename)s:%(lineno)d: %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S%z',
    ))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.DEBUG)
    # Standard startup stanza so we know what is running.
    logging.info('starting, version %s, argv: %s', version, sys.argv)


def main(args=None):
    error.install_signal_handler();
    if not args:
        args = sys.argv[1:]
    options = _parse_config(args)
    setup_logging(options.log_name, options.set_version)

    if options.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

    try:
        with _secret_auth_token_and_ssh_key(options) as (auth_token, ssh_key_file):
            api = gitlab.Api(options.gitlab_url, auth_token)
            user = user_module.User.myself(api)
            if options.max_ci_time_in_minutes:
                logging.warning(
                    "--max-ci-time-in-minutes is DEPRECATED, use --ci-timeout %dmin",
                    options.max_ci_time_in_minutes
                )
                options.ci_timeout = timedelta(minutes=options.max_ci_time_in_minutes)

            if options.batch:
                logging.warning('Experimental batch mode enabled')

            config = bot.BotConfig(
                user=user,
                ssh_key_file=ssh_key_file,
                project_regexp=options.project_regexp,
                git_timeout=options.git_timeout,
                git_reference_repo=options.git_reference_repo,
                branch_regexp=options.branch_regexp,
                merge_opts=bot.MergeJobOptions.default(
                    add_tested=options.add_tested,
                    add_part_of=options.add_part_of,
                    add_reviewers=options.add_reviewers,
                    reapprove=options.impersonate_approvers,
                    approval_timeout=options.approval_reset_timeout,
                    embargo=options.embargo,
                    ci_timeout=options.ci_timeout,
                    ci_timeout_skip=options.ci_timeout_skip,
                    merge_strategy=options.merge_strategy,
                    require_ci_run_by_me=options.require_ci_run_by_me,
                ),
                batch=options.batch,
                skip_pending=options.skip_pending,
                priority_labels=options.priority_labels.split(','),
            )

            marge_bot = bot.Bot(api=api, config=config)
            marge_bot.start()
    except error.SignalError as exc:
        logging.info('died on signal: %s' % (exc.signal,))
        sys.exit(exc.signal)
    except Exception as exc:
        logging.exception('died on exception')
        throw
