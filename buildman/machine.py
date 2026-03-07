# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Simon Glass <sjg@chromium.org>

"""Handles remote machine probing and pool management for distributed builds

This module provides the Machine and MachinePool classes for managing a pool of
remote build machines. Machines are probed over SSH to determine their
capabilities (CPUs, memory, load, toolchains) and can be used to distribute
board builds across multiple hosts.
"""

import dataclasses
import json
import os
import threading

from buildman import bsettings
from buildman import toolchain as toolchain_mod
from u_boot_pylib import command
from u_boot_pylib import terminal
from u_boot_pylib import tout

# Probe script run on remote machines via SSH. This is kept minimal so that
# it works on any Linux machine with Python 3.
PROBE_SCRIPT = r'''
import json, os, platform, subprocess

def get_cpus():
    try:
        return int(subprocess.check_output(['nproc', '--all'], text=True))
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return 1

def get_threads():
    try:
        return int(subprocess.check_output(['nproc'], text=True))
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return get_cpus()

def get_load():
    try:
        with open('/proc/loadavg') as f:
            return float(f.read().split()[0])
    except (IOError, ValueError, IndexError):
        return 0.0

def get_mem_avail_mb():
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except (IOError, ValueError, IndexError):
        pass
    return 0

def get_disk_avail_mb(path='~'):
    path = os.path.expanduser(path)
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) // (1024 * 1024)
    except OSError:
        return 0

def get_bogomips():
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                lower = line.lower()
                if 'bogomips' in lower and ':' in lower:
                    return float(line.split(':')[1].strip())
    except (IOError, ValueError, IndexError):
        pass
    return 0.0

print(json.dumps({
    'arch': platform.machine(),
    'cpus': get_cpus(),
    'threads': get_threads(),
    'bogomips': get_bogomips(),
    'load_1m': get_load(),
    'mem_avail_mb': get_mem_avail_mb(),
    'disk_avail_mb': get_disk_avail_mb(),
}))
'''

# Load threshold: if load_1m / cpus exceeds this, the machine is busy
LOAD_THRESHOLD = 0.8

# Minimum available disk space in MB to use a machine
MIN_DISK_MB = 1000

# Minimum available memory in MB to use a machine
MIN_MEM_MB = 512

# SSH connect timeout in seconds
SSH_TIMEOUT = 10

# Shorter timeout for probing, since it should be fast
PROBE_TIMEOUT = 3


@dataclasses.dataclass
class MachineInfo:
    """Probe results for a remote machine

    Attributes:
        arch (str): Machine architecture (e.g. 'x86_64', 'aarch64')
        cpus (int): Number of physical CPU cores
        threads (int): Number of hardware threads
        bogomips (float): BogoMIPS from /proc/cpuinfo (single core)
        load (float): 1-minute load average
        mem_avail_mb (int): Available memory in MB
        disk_avail_mb (int): Available disk space in MB
    """
    arch: str = ''
    cpus: int = 0
    threads: int = 0
    bogomips: float = 0.0
    load: float = 0.0
    mem_avail_mb: int = 0
    disk_avail_mb: int = 0


class MachineError(Exception):
    """Error communicating with a remote machine"""


def _run_ssh(hostname, cmd, timeout=SSH_TIMEOUT, stdin_data=None):
    """Run a command on a remote machine via SSH

    Args:
        hostname (str): SSH hostname (user@host or just host)
        cmd (list of str): Command and arguments, passed after '--' to
            SSH. May be a single-element list with a shell command string
        timeout (int): Connection timeout in seconds
        stdin_data (str or None): Data to send to the command's stdin

    Returns:
        str: stdout from the command

    Raises:
        MachineError: if SSH connection fails or command returns non-zero
    """
    ssh_cmd = [
        'ssh',
        '-o', 'BatchMode=yes',
        '-o', f'ConnectTimeout={timeout}',
        '-o', 'StrictHostKeyChecking=accept-new',
        hostname,
        '--',
    ] + cmd
    try:
        result = command.run_pipe(
            [ssh_cmd], capture=True, capture_stderr=True,
            raise_on_error=False, stdin_data=stdin_data)
    except command.CommandExc as exc:
        raise MachineError(str(exc)) from exc

    if result.return_code:
        stderr = result.stderr.strip()
        if stderr:
            # Take last non-empty line as the real error
            lines = [l for l in stderr.splitlines()
                     if l.strip()]
            msg = lines[-1] if lines else stderr
            raise MachineError(f'SSH to {hostname}: {msg}')
        raise MachineError(
            f'SSH to {hostname} failed with code '
            f'{result.return_code}')

    return result.stdout


def gcc_version(gcc_path):
    """Extract the gcc version directory from a toolchain path

    Looks for a 'gcc-*-nolibc' component in the path, which is the
    standard naming convention for buildman-fetched toolchains.

    Args:
        gcc_path (str): Full path to gcc binary, e.g.
            '~/.buildman-toolchains/gcc-13.1.0-nolibc/aarch64-linux/
            bin/aarch64-linux-gcc'

    Returns:
        str or None: The version directory (e.g. 'gcc-13.1.0-nolibc'),
            or None if the path does not follow this convention
    """
    for part in gcc_path.split('/'):
        if part.startswith('gcc-') and 'nolibc' in part:
            return part
    return None


def _parse_toolchain_list(output):
    """Parse the output of 'buildman --list-tool-chains'

    Extracts architecture -> gcc path mapping from the output.

    Args:
        output (str): Output from buildman --list-tool-chains

    Returns:
        dict: Architecture name -> gcc path string
    """
    archs = {}
    in_list = False
    for line in output.splitlines():
        # The list starts after "List of available toolchains"
        if 'List of available toolchains' in line:
            in_list = True
            continue
        if in_list and ':' in line:
            parts = line.split(':', 1)
            if len(parts) == 2:
                arch = parts[0].strip()
                gcc = parts[1].strip()
                if arch and gcc and arch != 'None':
                    archs[arch] = gcc
        elif in_list and not line.strip():
            # Empty line ends the list
            break
    return archs


def _toolchain_status(mach, local_archs, local_gcc=None):
    """Get toolchain status text and colour for a machine

    Args:
        mach (Machine): Machine to check
        local_archs (set of str): Toolchain archs available on local host
        local_gcc (dict or None): arch -> gcc path on the local machine

    Returns:
        tuple: (str, colour) where colour is a terminal.Color constant
            or None for no colour
    """
    if not mach.toolchains:
        err = mach.tc_error
        if err:
            return 'fail', terminal.Color.RED
        if not mach.avail and not mach.info.arch:
            return '-', None
        return 'none', terminal.Color.YELLOW
    if not local_archs:
        return str(len(mach.toolchains)), None
    missing = local_archs - set(mach.toolchains.keys())

    # Check for version mismatches
    mismatched = 0
    if local_gcc:
        for arch, path in mach.toolchains.items():
            local_ver = gcc_version(local_gcc.get(arch, ''))
            if not local_ver:
                continue
            remote_ver = gcc_version(path)
            if remote_ver and remote_ver != local_ver:
                mismatched += 1

    parts = []
    if missing:
        parts.append(f'{len(missing)} missing')
    if mismatched:
        parts.append(f'{mismatched} wrong ver')
    if parts:
        return ', '.join(parts), terminal.Color.YELLOW
    return 'OK', terminal.Color.GREEN


def build_version_map(local_gcc):
    """Build a map of architecture -> gcc version directory

    Args:
        local_gcc (dict): arch -> gcc path

    Returns:
        dict: arch -> version string (e.g. 'gcc-13.1.0-nolibc')
    """
    versions = {}
    if local_gcc:
        for arch, path in local_gcc.items():
            ver = gcc_version(path)
            if ver:
                versions[arch] = ver
    return versions


def resolve_toolchain_aliases(gcc_dict):
    """Add toolchain-alias entries to a gcc dict

    Resolves [toolchain-alias] config entries (e.g. x86->i386, sh->sh4)
    so that board architectures using alias names are recognised.

    Args:
        gcc_dict (dict): arch -> gcc path, modified in place
    """
    for tag, value in bsettings.get_items('toolchain-alias'):
        if tag not in gcc_dict:
            for alias in value.split():
                if alias in gcc_dict:
                    gcc_dict[tag] = gcc_dict[alias]
                    break


def get_machines_config():
    """Get the list of machine hostnames from the config

    Returns:
        list of str: List of hostnames from [machines] section
    """
    items = bsettings.get_items('machines')
    return [value.strip() if value else name.strip()
            for name, value in items]


def do_probe_machines(col=None, fetch=False, buildman_path='buildman'):
    """Probe all configured machines and display their status

    This is the entry point for 'buildman --machines' when used without a
    build command. It probes all machines, checks their toolchains and
    prints a summary.

    Args:
        col (terminal.Color or None): Colour object for output
        fetch (bool): True to fetch missing toolchains
        buildman_path (str): Path to buildman on remote machines

    Returns:
        int: 0 on success, non-zero on failure
    """
    if not col:
        col = terminal.Color()

    machines = get_machines_config()
    if not machines:
        print(col.build(col.RED,
                        'No machines configured. Add a [machines] section '
                        'to ~/.buildman'))
        return 1

    # Get local toolchains for comparison. Only include cross-
    # toolchains under ~/.buildman-toolchains/ since system compilers
    # (sandbox, c89, c99) can't be probed or fetched remotely.
    local_toolchains = toolchain_mod.Toolchains()
    local_toolchains.get_settings(show_warning=False)
    local_toolchains.scan(verbose=False)
    home = os.path.expanduser('~')
    local_gcc = {arch: tc.gcc
                 for arch, tc in local_toolchains.toolchains.items()
                 if tc.gcc.startswith(home)}
    resolve_toolchain_aliases(local_gcc)
    local_archs = set(local_gcc.keys())

    pool = MachinePool()
    pool.probe_all(col)
    pool.check_toolchains(local_archs, buildman_path=buildman_path,
                          fetch=fetch, col=col, local_gcc=local_gcc)
    pool.print_summary(col, local_archs=local_archs,
                        local_gcc=local_gcc)
    return 0


class MachinePool:
    """Manages a pool of remote build machines

    Reads machine hostnames from the [machines] section of the buildman
    config and provides methods to probe, check toolchains and display
    the status of all machines.

    Attributes:
        machines (list of Machine): All machines in the pool
    """

    def __init__(self, names=None):
        """Create a MachinePool

        Args:
            names (list of str or None): If provided, only include machines
                whose config key matches one of these names. If None, include
                all machines from the config.
        """
        self.machines = []
        self._load_from_config(names)

    def _load_from_config(self, names=None):
        """Load machine hostnames from the [machines] config section

        Supports bare hostnames (one per line) or name=hostname pairs.
        The hostname may include a username (user@host):
            [machines]
            ohau
            moa
            myserver = build1.example.com
            ruru = sglass@ruru

        Args:
            names (list of str or None): If provided, only include machines
                whose config key matches one of these names
        """
        name_set = set(names) if names else set()
        items = bsettings.get_items('machines')
        for name, value in items:
            # With allow_no_value=True, bare hostnames have value=None
            # and the hostname is the key. For key=value pairs, use value.
            key = name.strip()
            if name_set and key not in name_set:
                continue
            hostname = value.strip() if value else key
            mach = Machine(hostname, name=key)
            for oname, ovalue in bsettings.get_items(f'machine:{key}'):
                if oname == 'max_boards':
                    mach.max_boards = int(ovalue)
            self.machines.append(mach)

    def probe_all(self, col=None):
        """Probe all machines in the pool in parallel

        All machines are probed concurrently via threads. Progress is shown
        on a single line and results are printed afterwards.

        Args:
            col (terminal.Color or None): Colour object for output

        Returns:
            list of Machine: Machines that are available
        """
        if not col:
            col = terminal.Color()

        names = [m.name for m in self.machines]
        done = []
        lock = threading.Lock()

        def _probe(mach):
            mach.probe()
            with lock:
                done.append(mach.name)
                tout.progress(f'Probing {len(done)}/{len(names)}: '
                              f'{", ".join(done)}')

        # Probe all machines in parallel
        threads = []
        tout.progress(f'Probing {len(names)} machines')
        for mach in self.machines:
            t = threading.Thread(target=_probe, args=(mach,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        tout.clear_progress()
        return self.get_available()

    def check_toolchains(self, needed_archs, buildman_path='buildman',
                         fetch=False, col=None, local_gcc=None):
        """Check and optionally fetch toolchains on available machines

        Probes toolchains on all available machines in parallel. If
        fetch is True, missing toolchains are fetched sequentially.

        Toolchains whose gcc version (e.g. gcc-13.1.0-nolibc) differs
        from the local machine are treated as missing and will be
        re-fetched if fetch is True.

        Args:
            needed_archs (set of str): Set of architectures needed
                (e.g. {'arm', 'aarch64', 'sandbox'})
            buildman_path (str): Path to buildman on remote machines
            fetch (bool): True to attempt to fetch missing toolchains
            col (terminal.Color or None): Colour object for output
            local_gcc (dict or None): arch -> gcc path on the local
                machine, used for version comparison

        Returns:
            dict: Machine -> set of missing architectures
        """
        if not col:
            col = terminal.Color()

        reachable = self.get_reachable()
        if not reachable:
            return {}

        # Probe toolchains on all reachable machines, not just available
        # ones, so that busy machines still show toolchain info
        done = []
        lock = threading.Lock()

        def _check(mach):
            mach.probe_toolchains(buildman_path, local_gcc=local_gcc)
            with lock:
                done.append(mach.name)
                tout.progress(f'Checking toolchains {len(done)}/'
                              f'{len(reachable)}: {", ".join(done)}')

        threads = []
        tout.progress(f'Checking toolchains on {len(reachable)} machines')
        for mach in reachable:
            t = threading.Thread(target=_check, args=(mach,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        tout.clear_progress()

        local_versions = build_version_map(local_gcc)

        # Check for missing or version-mismatched toolchains
        missing_map = {}
        for mach in reachable:
            missing = needed_archs - set(mach.toolchains.keys())
            # Also treat version mismatches as missing
            for arch, path in mach.toolchains.items():
                local_ver = local_versions.get(arch)
                if not local_ver:
                    continue
                remote_ver = gcc_version(path)
                if remote_ver and remote_ver != local_ver:
                    missing.add(arch)
            if missing:
                missing_map[mach] = missing

        if fetch and missing_map:
            self._fetch_all_missing(missing_map, local_versions,
                                    local_gcc, buildman_path)

        return missing_map

    def _fetch_all_missing(self, missing_map, local_versions,
                           local_gcc, buildman_path):
        """Fetch missing toolchains on all machines in parallel

        For version-mismatched toolchains, removes the old version
        directory on the remote before fetching, so the new version
        takes its place.

        Updates missing_map in place, removing architectures that
        were successfully fetched.

        Args:
            missing_map (dict): Machine -> set of missing archs
            local_versions (dict): arch -> version string (e.g.
                'gcc-13.1.0-nolibc') from the local machine
            local_gcc (dict or None): arch -> gcc path on the boss,
                passed to re-probe after fetching
            buildman_path (str): Path to buildman on remote
        """
        lock = threading.Lock()
        done = []
        failed = []
        total = sum(len(v) for v in missing_map.values())

        def _fetch_one(mach, missing):
            fetched = set()
            for arch in list(missing):
                # Remove old mismatched version before fetching
                old_ver = gcc_version(mach.toolchains.get(arch, ''))
                if old_ver and old_ver != local_versions.get(arch):
                    try:
                        _run_ssh(mach.name, [
                            'rm', '-rf',
                            f'~/.buildman-toolchains/{old_ver}'])
                    except MachineError:
                        pass
                ok = mach.fetch_toolchain(buildman_path, arch)
                with lock:
                    done.append(arch)
                    if ok:
                        fetched.add(arch)
                    else:
                        failed.append(f'{mach.name}: {arch}')
                    tout.progress(
                        f'Fetching toolchains {len(done)}/{total}: '
                        f'{mach.name} {arch}')
            if fetched:
                mach.probe_toolchains(buildman_path,
                                      local_gcc=local_gcc)
                missing -= fetched
                if not missing:
                    with lock:
                        del missing_map[mach]

        tout.progress(f'Fetching {total} toolchains on '
                      f'{len(missing_map)} machines')
        threads = []
        for mach, missing in list(missing_map.items()):
            t = threading.Thread(target=_fetch_one, args=(mach, missing))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        tout.clear_progress()

        # Report failures
        for msg in failed:
            print(f'  Failed to fetch {msg}')

        # Report remaining version mismatches grouped by machine
        if missing_map:
            print('Version mismatches (local vs remote):')
            for mach, missing in sorted(missing_map.items(),
                                        key=lambda x: x[0].name):
                diffs = []
                for arch in sorted(missing):
                    local_ver = local_versions.get(arch, '?')
                    diffs.append(f'{arch}({local_ver})')
                print(f'  {mach.name}: {", ".join(diffs)}')

    def get_reachable(self):
        """Get list of machines that were successfully probed

        This includes machines that are busy or low on resources, as long
        as they were reachable via SSH.

        Returns:
            list of Machine: Reachable machines (may not be available)
        """
        return [m for m in self.machines
                if m.avail or m.info.arch]

    def get_available(self):
        """Get list of machines that are available for building

        Returns:
            list of Machine: Available machines
        """
        return [m for m in self.machines if m.avail]

    def get_total_weight(self):
        """Get the total weight of all available machines

        Returns:
            int: Sum of weights of all available machines
        """
        return sum(m.weight for m in self.get_available())

    def print_summary(self, col=None, local_archs=None, local_gcc=None):
        """Print a summary of all machines in the pool

        Args:
            col (terminal.Color or None): Colour object for output
            local_archs (set of str or None): Toolchain architectures available
                on the local host, used to compare remote toolchain status
            local_gcc (dict or None): arch -> gcc path on local machine,
                for version comparison
        """
        if not col:
            col = terminal.Color()
        if not local_archs:
            local_archs = set()
        available = self.get_available()
        total_weight = self.get_total_weight()
        print(col.build(col.BLUE,
              f'Machine pool: {len(available)} of {len(self.machines)} '
              f'machines available, total weight {total_weight}'))
        print()
        fmt = '  {:<10} {:>10} {:>7} {:>8} {:>6} {:>7} {:>7} {:>10}  {}'
        print(fmt.format('Name', 'Arch', 'Threads', 'BogoMIPS',
                         'Load', 'Mem GB', 'Disk TB', 'Toolchains', 'Status'))
        print(f'  {"-" * 88}')
        for mach in self.machines:
            if mach.avail:
                parts = [f'weight {mach.weight}']
                if mach.max_boards:
                    parts.append(f'max {mach.max_boards}')
                status_text = ', '.join(parts)
                status_colour = col.GREEN
            elif mach.reason == 'not probed':
                status_text = 'not probed'
                status_colour = col.YELLOW
            else:
                status_text = mach.reason
                status_colour = col.RED
            inf = mach.info
            mem_gb = f'{inf.mem_avail_mb / 1024:.1f}'
            disk_tb = f'{inf.disk_avail_mb / 1024 / 1024:.1f}'
            tc_text, tc_colour = _toolchain_status(
                mach, local_archs, local_gcc)

            # Format the line with plain text for correct alignment,
            # then apply colour to the toolchain and status fields
            line = fmt.format(mach.name, inf.arch or '-', inf.threads,
                              f'{inf.bogomips:.0f}', f'{inf.load:.1f}',
                              mem_gb, disk_tb, tc_text, status_text)
            if tc_colour:
                line = line.replace(tc_text,
                                    col.build(tc_colour, tc_text), 1)
            line = line.replace(status_text,
                                col.build(status_colour, status_text), 1)
            print(line)

        local_versions = build_version_map(local_gcc)

        # Print toolchain errors and missing details after the table
        notes = []
        for mach in self.machines:
            err = mach.tc_error
            if err:
                notes.append(f'  {mach.name}: {err}')
            elif local_archs and mach.toolchains is not None:
                missing = local_archs - set(mach.toolchains.keys())
                if missing:
                    parts = []
                    for arch in sorted(missing):
                        ver = local_versions.get(arch)
                        if ver:
                            parts.append(f'{arch}({ver})')
                        else:
                            parts.append(arch)
                    notes.append(
                        f'  {mach.name}: need {", ".join(parts)}')
        if notes:
            print()
            for note in notes:
                print(note)


class Machine:
    """Represents a remote (or local) build machine

    Attributes:
        hostname (str): SSH hostname (user@host or just host)
        name (str): Short display name from config key
        info (MachineInfo): Probed machine information
        avail (bool): True if reachable and not too busy
        reason (str): Reason the machine is unavailable, or ''
        toolchains (dict): Available toolchain architectures, arch -> gcc path
        tc_error (str): Error from last toolchain probe, or ''
        weight (int): Number of build threads to allocate
        max_boards (int): Max concurrent boards (0 = use nthreads)
    """
    def __init__(self, hostname, name=None):
        self.hostname = hostname
        self.name = name or hostname
        self.info = MachineInfo()
        self.avail = False
        self.reason = 'not probed'
        self.toolchains = {}
        self.tc_error = ''
        self.weight = 0
        self.max_boards = 0

    def probe(self, timeout=PROBE_TIMEOUT):
        """Probe this machine's capabilities over SSH

        Runs a small Python script on the remote machine to collect
        architecture, CPU count, thread count, load average, available memory
        and disk space.

        Args:
            timeout (int): SSH connect timeout in seconds

        Returns:
            bool: True if the machine was probed successfully
        """
        try:
            result = _run_ssh(self.hostname, ['python3'],
                              timeout=timeout, stdin_data=PROBE_SCRIPT)
        except MachineError as exc:
            self.avail = False
            self.reason = str(exc)
            return False

        try:
            info = json.loads(result)
        except json.JSONDecodeError:
            self.avail = False
            self.reason = f'invalid probe response: {result[:100]}'
            return False

        self.info = MachineInfo(
            arch=info.get('arch', ''),
            cpus=info.get('cpus', 0),
            threads=info.get('threads', 0),
            bogomips=info.get('bogomips', 0.0),
            load=info.get('load_1m', 0.0),
            mem_avail_mb=info.get('mem_avail_mb', 0),
            disk_avail_mb=info.get('disk_avail_mb', 0),
        )

        # Check whether the machine is too busy or low on resources
        self.avail = True
        self.reason = ''
        inf = self.info
        if inf.cpus and inf.load / inf.cpus > LOAD_THRESHOLD:
            self.avail = False
            self.reason = (f'busy (load {inf.load:.1f} '
                           f'with {inf.cpus} cpus)')
        elif inf.disk_avail_mb < MIN_DISK_MB:
            self.avail = False
            self.reason = (f'low disk '
                           f'({inf.disk_avail_mb} MB available)')
        elif inf.mem_avail_mb < MIN_MEM_MB:
            self.avail = False
            self.reason = (f'low memory '
                           f'({inf.mem_avail_mb} MB available)')

        if self.avail:
            self._calc_weight()
        return True

    def probe_toolchains(self, buildman_path, local_gcc=None):
        """Probe available toolchains on this machine

        If local_gcc is provided, checks which of the boss's
        toolchains exist on this machine by testing for the gcc
        binary under ~/.buildman-toolchains. This avoids depending
        on the remote machine's .buildman config.

        Falls back to running 'buildman --list-tool-chains' on the
        remote when local_gcc is not provided (e.g. --machines
        without a build).

        Args:
            buildman_path (str): Path to buildman on the remote machine
            local_gcc (dict or None): arch -> gcc path on the boss

        Returns:
            dict: Architecture -> gcc path mapping
        """
        self.tc_error = ''
        if local_gcc:
            return self._probe_toolchains_from_boss(local_gcc)
        try:
            result = _run_ssh(self.hostname,
                              [buildman_path, '--list-tool-chains'])
        except MachineError as exc:
            self.toolchains = {}
            self.tc_error = str(exc)
            return self.toolchains

        self.toolchains = _parse_toolchain_list(result)
        return self.toolchains

    def _probe_toolchains_from_boss(self, local_gcc):
        """Check which of the boss's toolchains exist on this machine

        For each architecture, extracts the path relative to the home
        directory (e.g. .buildman-toolchains/gcc-13.1.0-nolibc/...)
        and tests whether that gcc binary exists on the remote. This
        makes the worker mirror the boss's toolchain choices.

        Args:
            local_gcc (dict): arch -> gcc path on the boss

        Returns:
            dict: Architecture -> gcc path mapping (using remote paths)
        """
        # Build a list of relative paths to check
        home_prefix = os.path.expanduser('~')
        checks = {}
        for arch, gcc in local_gcc.items():
            if gcc.startswith(home_prefix):
                rel = gcc[len(home_prefix):]
                if rel.startswith('/'):
                    rel = rel[1:]
                checks[arch] = rel

        if not checks:
            self.toolchains = {}
            return self.toolchains

        # Build a single SSH command that tests all paths
        # Output: "arch:yes" or "arch:no" for each
        test_cmds = []
        for arch, rel in checks.items():
            test_cmds.append(
                f'test -f ~/{rel} && echo {arch}:yes || echo {arch}:no')
        try:
            result = _run_ssh(self.hostname,
                              ['; '.join(test_cmds)])
        except MachineError as exc:
            self.toolchains = {}
            self.tc_error = str(exc)
            return self.toolchains

        self.toolchains = {}
        for line in result.splitlines():
            line = line.strip()
            if ':yes' in line:
                arch = line.split(':')[0]
                rel = checks.get(arch)
                if rel:
                    self.toolchains[arch] = f'~/{rel}'
        return self.toolchains

    def fetch_toolchain(self, buildman_path, arch):
        """Fetch a toolchain for a given architecture on this machine

        Args:
            buildman_path (str): Path to buildman on the remote
            arch (str): Architecture to fetch (e.g. 'arm')

        Returns:
            bool: True if the fetch succeeded
        """
        try:
            _run_ssh(self.hostname,
                     [buildman_path, '--fetch-arch', arch])
            return True
        except MachineError:
            return False

    def _calc_weight(self):
        """Calculate the build weight (threads to allocate) for this machine

        Uses available threads minus a fraction of the current load to avoid
        over-committing a partially loaded machine.
        """
        if not self.avail:
            self.weight = 0
            return
        # Reserve some capacity based on current load
        spare = max(1, self.info.threads - int(self.info.load))
        self.weight = spare

    def __repr__(self):
        inf = self.info
        status = 'avail' if self.avail else f'unavail: {self.reason}'
        return (f'Machine({self.hostname}, arch={inf.arch}, '
                f'threads={inf.threads}, '
                f'bogomips={inf.bogomips:.0f}, load={inf.load:.1f}, '
                f'weight={self.weight}, {status})')
