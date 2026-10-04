# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

"""Common Claude Agent SDK utilities.

Provides shared functions for running Claude agents across tools that need
AI assistance (e.g. pickman, patman review).
"""

import os
import shutil
import subprocess

from u_boot_pylib import tout

# Markers in an agent failure which mean every later call will fail too: the
# tool is too old, or not signed in, or asked for a model it cannot serve.
# Retrying these achieves nothing, so a caller which loops needs to know
FATAL_MARKERS = (
    'does not support this model',
    'claude_code_version_too_old',
    'is required. Run \'claude update\'',
    'authentication_error',
    'invalid_api_key',
    'Please run /login',
)


# Set once a failure is seen which will repeat, so that a caller which loops
# can stop instead of retrying the same thing for ever
fatal_seen = None  # pylint: disable=invalid-name


def find_cli():
    """Find the Claude Code binary to run

    The SDK prefers a copy bundled inside claude_agent_sdk over anything on
    the path, and 'claude update' never touches that copy, so left to itself
    it runs whatever was current when the SDK was installed.  Pick the newer
    of the two instead, so that updating either one is enough.  CLAUDE_CLI
    overrides the lot.

    Return:
        tuple:
            str: Path to the binary, or None if none was found
            bool: True if it is the copy bundled with the SDK
    """
    chosen = os.environ.get('CLAUDE_CLI')
    if chosen:
        return chosen, False
    on_path = shutil.which('claude')
    bundled = find_bundled_cli()
    if not bundled:
        return on_path, False
    if not on_path:
        return bundled, True
    if parse_version(get_cli_version(on_path)) > parse_version(
            get_cli_version(bundled)):
        return on_path, False
    return bundled, True


def find_bundled_cli():
    """Find the Claude Code binary bundled with the SDK, if there is one

    Return:
        str: Path to the binary, or None if the SDK or its copy is missing
    """
    try:
        import claude_agent_sdk  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None
    bundled = os.path.join(os.path.dirname(claude_agent_sdk.__file__),
                           '_bundled', 'claude')
    return bundled if os.path.isfile(bundled) else None


def parse_version(version):
    """Turn a version string into something that sorts

    Args:
        version (str): Version such as '2.1.260', or None

    Return:
        tuple: Numeric parts of the version, empty if it could not be read
    """
    if not version:
        return ()
    parts = []
    for part in version.split('.'):
        digits = ''.join(c for c in part if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def get_cli_version(path):
    """Get the version a Claude Code binary reports

    Args:
        path (str): Path to the binary

    Return:
        str: Version string, or None if it could not be read
    """
    try:
        out = subprocess.run([path, '--version'], capture_output=True,
                             text=True, timeout=30, check=False).stdout
        return out.strip().split()[0] if out.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def describe_cli():
    """Say which Claude Code binary will run, and which version

    Where it came from matters as much as the version: a copy bundled with
    the SDK is not updated by 'claude update', so an old one can sit there
    while the shell reports something newer.

    Return:
        str: A line naming the version and where it came from, or None
    """
    path, bundled = find_cli()
    if not path:
        return None
    version = get_cli_version(path) or 'unknown version'
    where = 'bundled with claude-agent-sdk' if bundled else path
    return f'Claude Code {version} ({where})'


def is_fatal_error(text):
    """Check whether an agent failure will repeat on every future call

    Args:
        text (str): The error text

    Return:
        bool: True if the failure is in the setup rather than the request
    """
    return any(mark in text for mark in FATAL_MARKERS)

# Maximum buffer size for agent responses
MAX_BUFFER_SIZE = 10 * 1024 * 1024  # 10MB

# Check if claude_agent_sdk is available
try:
    from claude_agent_sdk import query, ClaudeAgentOptions
    AGENT_AVAILABLE = True
except ImportError:
    AGENT_AVAILABLE = False


# Set once the binary in use has been named, so that a run which starts
# several agents says it once rather than before each
announced = False  # pylint: disable=invalid-name


def cli_path_option():
    """Give the cli_path argument for ClaudeAgentOptions, if one is wanted

    The SDK finds its own bundled copy without help, so the path is only
    passed when something else should run instead.

    Return:
        dict: {'cli_path': ...} when a binary other than the bundled one is
            wanted, else {}
    """
    path, bundled = find_cli()
    return {'cli_path': path} if path and not bundled else {}


def announce_cli():
    """Say once which Claude Code binary is in use

    Worth stating rather than leaving to be worked out: when the version is
    wrong the failure names a version nobody can find with 'claude --version'.
    """
    global announced  # pylint: disable=global-statement
    if announced:
        return
    announced = True
    desc = describe_cli()
    if desc:
        tout.info(f'Using {desc}')


def check_available():
    """Check if the Claude Agent SDK is available

    Returns:
        bool: True if available, False otherwise
    """
    if not AGENT_AVAILABLE:
        tout.error('Claude Agent SDK not available')
        tout.error('Install with: pip install claude-agent-sdk')
        return False
    return True


async def run_agent_collect(prompt, options):
    """Run a Claude agent and collect its conversation log

    Sends the prompt to a Claude agent, streams output to stdout and
    collects all text blocks into a conversation log.

    Args:
        prompt (str): The prompt to send to the agent
        options (ClaudeAgentOptions): Agent configuration

    Returns:
        tuple: (success, conversation_log) where success is bool and
            conversation_log is the agent's output text
    """
    debug = os.environ.get('PATMAN_DEBUG_AGENT')
    announce_cli()
    conversation_log = []
    try:
        async for message in query(prompt=prompt, options=options):
            if debug:
                tout.error(f'AGENT MSG: {type(message).__name__}: '
                           f'{message!r}')
            if hasattr(message, 'content'):
                for block in message.content:
                    if debug:
                        tout.error(f'  BLOCK: {type(block).__name__}: '
                                   f'{block!r}')
                    if hasattr(block, 'text'):
                        print(block.text)
                        conversation_log.append(block.text)
        return True, '\n\n'.join(conversation_log)
    except (RuntimeError, ValueError, OSError) as exc:
        _report_failure(exc)
        return False, '\n\n'.join(conversation_log)
    except Exception as exc:
        if 'API Error' in str(exc) or 'exit code' in str(exc):
            _report_failure(exc)
            return False, '\n\n'.join(conversation_log)
        raise


def _report_failure(exc):
    """Report an agent failure, saying if it will repeat

    Args:
        exc (Exception): The failure
    """
    global fatal_seen  # pylint: disable=global-statement
    tout.error(f'Agent failed: {exc}')
    if is_fatal_error(str(exc)):
        fatal_seen = str(exc)
        tout.error('This is a problem with the setup rather than with this '
                   'request, so every later agent call will fail the same '
                   'way until it is put right')
        _, bundled = find_cli()
        if bundled:
            tout.error("The binary is bundled inside claude-agent-sdk, so "
                       "'claude update' will not touch it - update the SDK "
                       'itself, or point at another with the CLAUDE_CLI '
                       'environment variable')
