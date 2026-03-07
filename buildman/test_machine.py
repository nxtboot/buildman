# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Simon Glass <sjg@chromium.org>

"""Tests for the machine module"""

# pylint: disable=W0212

import json
import os
import unittest
from unittest import mock

from u_boot_pylib import command
from u_boot_pylib import terminal

from buildman import bsettings
from buildman import machine


# Base machine-info dict used by probe tests. Individual tests override
# fields as needed (e.g. load_1m, mem_avail_mb) via {**MACHINE_INFO, ...}.
MACHINE_INFO = {
    'arch': 'x86_64',
    'cpus': 4,
    'threads': 8,
    'bogomips': 5000.0,
    'load_1m': 0.5,
    'mem_avail_mb': 16000,
    'disk_avail_mb': 50000,
}


class TestParseToolchainList(unittest.TestCase):
    """Test _parse_toolchain_list()"""

    def test_parse_normal(self):
        """Test parsing normal toolchain list output"""
        output = '''List of available toolchains (3):
arm       : /usr/bin/arm-linux-gnueabi-gcc
aarch64   : /usr/bin/aarch64-linux-gnu-gcc
sandbox   : /usr/bin/gcc
'''
        result = machine._parse_toolchain_list(output)
        self.assertEqual(result, {
            'arm': '/usr/bin/arm-linux-gnueabi-gcc',
            'aarch64': '/usr/bin/aarch64-linux-gnu-gcc',
            'sandbox': '/usr/bin/gcc',
        })

    def test_parse_empty(self):
        """Test parsing empty output"""
        self.assertEqual(machine._parse_toolchain_list(''), {})

    def test_parse_none_toolchains(self):
        """Test parsing when no toolchains are available"""
        output = '''List of available toolchains (0):
None
'''
        result = machine._parse_toolchain_list(output)
        self.assertEqual(result, {})

    def test_parse_with_colour(self):
        """Test parsing output that has extra text before the list"""
        output = """Some preamble text
List of available toolchains (2):
arm       : /opt/toolchains/arm-gcc
x86       : /usr/bin/x86_64-linux-gcc

Some trailing text
"""
        result = machine._parse_toolchain_list(output)
        self.assertEqual(result, {
            'arm': '/opt/toolchains/arm-gcc',
            'x86': '/usr/bin/x86_64-linux-gcc',
        })


class TestMachine(unittest.TestCase):
    """Test Machine class"""

    def test_init(self):
        """Test initial state of a Machine"""
        m = machine.Machine('myhost')
        self.assertEqual(m.hostname, 'myhost')
        self.assertEqual(m.info.arch, '')
        self.assertFalse(m.avail)
        self.assertEqual(m.reason, 'not probed')
        self.assertEqual(m.weight, 0)
        self.assertEqual(m.toolchains, {})

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_success(self, mock_ssh):
        """Test successful probe"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'cpus': 8, 'threads': 16, 'load_1m': 1.5})
        m = machine.Machine('server1')
        result = m.probe()
        self.assertTrue(result)
        self.assertTrue(m.avail)
        self.assertEqual(m.info.cpus, 8)
        self.assertEqual(m.info.threads, 16)
        self.assertAlmostEqual(m.info.load, 1.5)
        self.assertEqual(m.info.mem_avail_mb, 16000)
        self.assertEqual(m.info.disk_avail_mb, 50000)
        self.assertGreater(m.weight, 0)

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_busy(self, mock_ssh):
        """Test probe of a busy machine"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'load_1m': 5.0})
        m = machine.Machine('busy-host')
        result = m.probe()
        self.assertTrue(result)
        self.assertFalse(m.avail)
        self.assertIn('busy', m.reason)

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_low_disk(self, mock_ssh):
        """Test probe of a machine with low disk space"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'disk_avail_mb': 500})
        m = machine.Machine('low-disk')
        result = m.probe()
        self.assertTrue(result)
        self.assertFalse(m.avail)
        self.assertIn('disk', m.reason)

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_low_mem(self, mock_ssh):
        """Test probe of a machine with low memory"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'mem_avail_mb': 200})
        m = machine.Machine('low-mem')
        result = m.probe()
        self.assertTrue(result)
        self.assertFalse(m.avail)
        self.assertIn('memory', m.reason)

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_ssh_failure(self, mock_ssh):
        """Test probe when SSH fails"""
        mock_ssh.side_effect = machine.MachineError('connection refused')
        m = machine.Machine('bad-host')
        result = m.probe()
        self.assertFalse(result)
        self.assertFalse(m.avail)
        self.assertIn('connection refused', m.reason)

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_bad_json(self, mock_ssh):
        """Test probe when remote returns invalid JSON"""
        mock_ssh.return_value = 'not json at all'
        m = machine.Machine('bad-json')
        result = m.probe()
        self.assertFalse(result)
        self.assertFalse(m.avail)
        self.assertIn('invalid probe response', m.reason)

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_toolchains(self, mock_ssh):
        """Test probing toolchains"""
        mock_ssh.return_value = '''List of available toolchains (2):
arm       : /usr/bin/arm-linux-gnueabi-gcc
sandbox   : /usr/bin/gcc

'''
        m = machine.Machine('server1')
        archs = m.probe_toolchains('buildman')
        self.assertEqual(archs, {
            'arm': '/usr/bin/arm-linux-gnueabi-gcc',
            'sandbox': '/usr/bin/gcc',
        })

    @mock.patch('buildman.machine._run_ssh')
    def test_fetch_toolchain_success(self, mock_ssh):
        """Test successful toolchain fetch"""
        mock_ssh.return_value = 'Downloading...\nDone'
        m = machine.Machine('server1')
        self.assertTrue(m.fetch_toolchain('buildman', 'arm'))

    @mock.patch('buildman.machine._run_ssh')
    def test_fetch_toolchain_failure(self, mock_ssh):
        """Test failed toolchain fetch"""
        mock_ssh.side_effect = machine.MachineError('fetch failed')
        m = machine.Machine('server1')
        self.assertFalse(m.fetch_toolchain('buildman', 'arm'))

    @mock.patch('buildman.machine._run_ssh')
    def test_weight_calculation(self, mock_ssh):
        """Test weight calculation based on load"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'cpus': 8, 'threads': 16, 'load_1m': 4.0})
        m = machine.Machine('server1')
        m.probe()
        # weight = threads - int(load) = 16 - 4 = 12
        self.assertEqual(m.weight, 12)

    @mock.patch('buildman.machine._run_ssh')
    def test_weight_minimum(self, mock_ssh):
        """Test weight is at least 1 when available"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'arch': 'aarch64', 'threads': 4,
            'bogomips': 48.0, 'load_1m': 3.1})
        m = machine.Machine('server1')
        m.probe()
        # weight = max(1, 4 - 3) = 1
        self.assertEqual(m.weight, 1)

    def test_repr(self):
        """Test string representation"""
        m = machine.Machine('server1')
        self.assertIn('server1', repr(m))
        self.assertIn('unavail', repr(m))


class TestMachinePool(unittest.TestCase):
    """Test MachinePool class"""

    def setUp(self):
        """Set up bsettings for each test"""
        bsettings.setup(None)

    def test_empty_pool(self):
        """Test pool with no machines configured"""
        pool = machine.MachinePool()
        self.assertEqual(pool.machines, [])
        self.assertEqual(pool.get_available(), [])
        self.assertEqual(pool.get_total_weight(), 0)

    def test_load_from_config(self):
        """Test loading machines from config with bare hostnames"""
        bsettings.add_file(
            '[machines]\n'
            'ohau\n'
            'moa\n'
        )
        pool = machine.MachinePool()
        self.assertEqual(len(pool.machines), 2)
        self.assertEqual(pool.machines[0].hostname, 'ohau')
        self.assertEqual(pool.machines[1].hostname, 'moa')

    def test_load_from_config_key_value(self):
        """Test loading machines from config with key=value pairs"""
        bsettings.add_file(
            '[machines]\n'
            'server1 = build1.example.com\n'
            'server2 = user@build2.example.com\n'
        )
        pool = machine.MachinePool()
        self.assertEqual(len(pool.machines), 2)
        self.assertEqual(pool.machines[0].hostname, 'build1.example.com')
        self.assertEqual(pool.machines[1].hostname, 'user@build2.example.com')

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_all(self, mock_ssh):
        """Test probing all machines"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'load_1m': 1.0,
            'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
        bsettings.add_file(
            '[machines]\n'
            'host1\n'
            'host2\n'
        )
        pool = machine.MachinePool()
        available = pool.probe_all()
        self.assertEqual(len(available), 2)
        self.assertEqual(pool.get_total_weight(), 14)

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_mixed(self, mock_ssh):
        """Test probing with some machines available and some not"""
        def ssh_side_effect(hostname, _cmd, **_kwargs):
            if hostname == 'host1':
                return json.dumps({
                    **MACHINE_INFO, 'load_1m': 1.0,
                    'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
            raise machine.MachineError('connection refused')

        mock_ssh.side_effect = ssh_side_effect
        bsettings.add_file(
            '[machines]\n'
            'host1\n'
            'host2\n'
        )
        pool = machine.MachinePool()
        available = pool.probe_all()
        self.assertEqual(len(available), 1)
        self.assertEqual(available[0].hostname, 'host1')

    @mock.patch('buildman.machine._run_ssh')
    def test_check_toolchains(self, mock_ssh):
        """Test checking toolchains on machines"""
        def ssh_side_effect(_hostname, cmd, **_kwargs):
            if 'python3' in cmd:
                return json.dumps({
                    **MACHINE_INFO, 'load_1m': 1.0,
                    'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
            if '--list-tool-chains' in cmd:
                return '''List of available toolchains (2):
arm       : /usr/bin/arm-gcc
sandbox   : /usr/bin/gcc

'''
            return ''

        mock_ssh.side_effect = ssh_side_effect
        bsettings.add_file(
            '[machines]\n'
            'host1\n'
        )
        pool = machine.MachinePool()
        pool.probe_all()
        missing = pool.check_toolchains({'arm', 'sandbox'})
        self.assertEqual(missing, {})

    @mock.patch('buildman.machine._run_ssh')
    def test_check_toolchains_missing(self, mock_ssh):
        """Test checking toolchains with some missing"""
        def ssh_side_effect(_hostname, cmd, **_kwargs):
            if 'python3' in cmd:
                return json.dumps({
                    **MACHINE_INFO, 'load_1m': 1.0,
                    'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
            if '--list-tool-chains' in cmd:
                return '''List of available toolchains (1):
sandbox   : /usr/bin/gcc

'''
            return ''

        mock_ssh.side_effect = ssh_side_effect
        bsettings.add_file(
            '[machines]\n'
            'host1\n'
        )
        pool = machine.MachinePool()
        pool.probe_all()
        missing = pool.check_toolchains({'arm', 'sandbox'})
        self.assertEqual(len(missing), 1)
        m = list(missing.keys())[0]
        self.assertIn('arm', missing[m])


class TestRunSsh(unittest.TestCase):
    """Test _run_ssh()"""

    @mock.patch('buildman.machine.command.run_pipe')
    def test_success(self, mock_pipe):
        """Test successful SSH command"""
        mock_pipe.return_value = mock.Mock(
            return_code=0, stdout='hello\n', stderr='')
        result = machine._run_ssh('host1', ['echo', 'hello'])
        self.assertEqual(result, 'hello\n')

        # Verify SSH options
        pipe_list = mock_pipe.call_args[0][0]
        cmd = pipe_list[0]
        self.assertIn('ssh', cmd)
        self.assertIn('BatchMode=yes', cmd)
        self.assertIn('host1', cmd)
        self.assertIn('echo', cmd)

    @mock.patch('buildman.machine.command.run_pipe')
    def test_failure(self, mock_pipe):
        """Test SSH command failure"""
        mock_pipe.return_value = mock.Mock(
            return_code=255, stdout='',
            stderr='Connection refused')
        with self.assertRaises(machine.MachineError) as ctx:
            machine._run_ssh('host1', ['echo', 'hello'])
        self.assertIn('Connection refused', str(ctx.exception))

    @mock.patch('buildman.machine.command.run_pipe')
    def test_failure_multiline_stderr(self, mock_pipe):
        """Test SSH failure with multi-line stderr picks last line"""
        mock_pipe.return_value = mock.Mock(
            return_code=255, stdout='',
            stderr='Warning: Added host key\n'
                   'Permission denied (publickey).')
        with self.assertRaises(machine.MachineError) as ctx:
            machine._run_ssh('host1', ['echo', 'hello'])
        self.assertIn('Permission denied', str(ctx.exception))
        self.assertNotIn('Warning', str(ctx.exception))

    @mock.patch('buildman.machine.command.run_pipe')
    def test_command_exc(self, mock_pipe):
        """Test SSH command exception"""
        mock_pipe.side_effect = command.CommandExc(
            'ssh failed', command.CommandResult())
        with self.assertRaises(machine.MachineError) as ctx:
            machine._run_ssh('host1', ['echo', 'hello'])
        self.assertIn('ssh failed', str(ctx.exception))


class TestGetMachinesConfig(unittest.TestCase):
    """Test get_machines_config()"""

    def setUp(self):
        bsettings.setup(None)

    def test_empty(self):
        """Test with no machines configured"""
        self.assertEqual(machine.get_machines_config(), [])

    def test_with_machines(self):
        """Test with machines configured"""
        bsettings.add_file(
            '[machines]\n'
            'host1\n'
            'host2\n'
        )
        result = machine.get_machines_config()
        self.assertEqual(result, ['host1', 'host2'])


class TestGccVersion(unittest.TestCase):
    """Test gcc_version()"""

    def test_normal_path(self):
        """Test extracting version from a standard toolchain path"""
        path = ('~/.buildman-toolchains/gcc-13.1.0-nolibc/'
                'aarch64-linux/bin/aarch64-linux-gcc')
        self.assertEqual(machine.gcc_version(path), 'gcc-13.1.0-nolibc')

    def test_no_match(self):
        """Test path that does not contain a gcc-*-nolibc component"""
        self.assertIsNone(machine.gcc_version('/usr/bin/gcc'))

    def test_empty(self):
        """Test empty path"""
        self.assertIsNone(machine.gcc_version(''))


class TestBuildVersionMap(unittest.TestCase):
    """Test build_version_map()"""

    def test_normal(self):
        """Test building version map from gcc dict"""
        gcc = {
            'arm': '~/.buildman-toolchains/gcc-13.1.0-nolibc/arm/bin/gcc',
            'sandbox': '/usr/bin/gcc',
        }
        result = machine.build_version_map(gcc)
        self.assertEqual(result, {'arm': 'gcc-13.1.0-nolibc'})

    def test_none(self):
        """Test with None input"""
        self.assertEqual(machine.build_version_map(None), {})


class TestResolveToolchainAliases(unittest.TestCase):
    """Test resolve_toolchain_aliases()"""

    def setUp(self):
        bsettings.setup(None)

    def test_alias(self):
        """Test resolving aliases from config"""
        bsettings.add_file(
            '[toolchain-alias]\n'
            'x86 = i386 i686\n'
        )
        gcc = {'i386': '/usr/bin/i386-gcc'}
        machine.resolve_toolchain_aliases(gcc)
        self.assertEqual(gcc['x86'], '/usr/bin/i386-gcc')

    def test_no_alias_needed(self):
        """Test when arch already exists"""
        bsettings.add_file(
            '[toolchain-alias]\n'
            'x86 = i386\n'
        )
        gcc = {'x86': '/usr/bin/x86-gcc', 'i386': '/usr/bin/i386-gcc'}
        machine.resolve_toolchain_aliases(gcc)
        # Should not overwrite existing
        self.assertEqual(gcc['x86'], '/usr/bin/x86-gcc')


class TestToolchainStatus(unittest.TestCase):
    """Test _toolchain_status()"""

    def test_no_toolchains_no_error(self):
        """Test machine with no toolchains and no error"""
        m = machine.Machine('host1')
        m.avail = True
        m.info.arch = 'x86_64'
        text, colour = machine._toolchain_status(m, set())
        self.assertEqual(text, 'none')

    def test_no_toolchains_with_error(self):
        """Test machine with toolchain error"""
        m = machine.Machine('host1')
        m.tc_error = 'SSH failed'
        text, colour = machine._toolchain_status(m, set())
        self.assertEqual(text, 'fail')

    def test_all_present(self):
        """Test all local toolchains present on machine"""
        m = machine.Machine('host1')
        m.toolchains = {'arm': '/usr/bin/arm-gcc',
                         'sandbox': '/usr/bin/gcc'}
        local = {'arm', 'sandbox'}
        text, colour = machine._toolchain_status(m, local)
        self.assertEqual(text, 'OK')

    def test_some_missing(self):
        """Test some toolchains missing"""
        m = machine.Machine('host1')
        m.toolchains = {'sandbox': '/usr/bin/gcc'}
        local = {'arm', 'sandbox'}
        text, colour = machine._toolchain_status(m, local)
        self.assertIn('missing', text)

    def test_version_mismatch(self):
        """Test version mismatch detection"""
        m = machine.Machine('host1')
        m.toolchains = {
            'arm': '~/.buildman-toolchains/gcc-12.0.0-nolibc/arm/bin/gcc'}
        local_gcc = {
            'arm': '~/.buildman-toolchains/gcc-13.1.0-nolibc/arm/bin/gcc'}
        text, colour = machine._toolchain_status(
            m, {'arm'}, local_gcc=local_gcc)
        self.assertIn('wrong ver', text)

    def test_no_local_archs(self):
        """Test with empty local arch set"""
        m = machine.Machine('host1')
        m.toolchains = {'arm': '/usr/bin/gcc', 'x86': '/usr/bin/gcc'}
        text, colour = machine._toolchain_status(m, set())
        self.assertEqual(text, '2')

    def test_unreachable_no_toolchains(self):
        """Test unreachable machine with no arch info"""
        m = machine.Machine('host1')
        text, colour = machine._toolchain_status(m, {'arm'})
        self.assertEqual(text, '-')


class TestMachineExtended(unittest.TestCase):
    """Extended Machine tests for coverage"""

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_toolchains_ssh_failure(self, mock_ssh):
        """Test toolchain probe when SSH fails"""
        mock_ssh.side_effect = machine.MachineError('timeout')
        m = machine.Machine('host1')
        result = m.probe_toolchains('buildman')
        self.assertEqual(result, {})
        self.assertIn('timeout', m.tc_error)

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_toolchains_from_boss(self, mock_ssh):
        """Test probing toolchains by checking boss's paths on remote"""
        home = os.path.expanduser('~')
        local_gcc = {
            'arm': f'{home}/.buildman-toolchains/gcc-13/arm/bin/gcc',
            'x86': f'{home}/.buildman-toolchains/gcc-13/x86/bin/gcc',
        }
        mock_ssh.return_value = 'arm:yes\nx86:no\n'
        m = machine.Machine('host1')
        result = m.probe_toolchains('buildman', local_gcc=local_gcc)
        self.assertIn('arm', result)
        self.assertNotIn('x86', result)

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_toolchains_from_boss_ssh_fail(self, mock_ssh):
        """Test probing boss toolchains when SSH fails"""
        home = os.path.expanduser('~')
        local_gcc = {
            'arm': f'{home}/.buildman-toolchains/gcc-13/arm/bin/gcc',
        }
        mock_ssh.side_effect = machine.MachineError('conn refused')
        m = machine.Machine('host1')
        result = m.probe_toolchains('buildman', local_gcc=local_gcc)
        self.assertEqual(result, {})
        self.assertIn('conn refused', m.tc_error)

    @mock.patch('buildman.machine._run_ssh')
    def test_probe_toolchains_from_boss_no_home(self, mock_ssh):
        """Test probing boss toolchains with non-home paths"""
        local_gcc = {'sandbox': '/usr/bin/gcc'}
        m = machine.Machine('host1')
        result = m.probe_toolchains('buildman', local_gcc=local_gcc)
        self.assertEqual(result, {})
        mock_ssh.assert_not_called()

    @mock.patch('buildman.machine._run_ssh')
    def test_fetch_toolchain_success(self, mock_ssh):
        """Test successful toolchain fetch"""
        mock_ssh.return_value = 'Fetched arm toolchain'
        m = machine.Machine('host1')
        self.assertTrue(m.fetch_toolchain('buildman', 'arm'))

    @mock.patch('buildman.machine._run_ssh')
    def test_fetch_toolchain_failure(self, mock_ssh):
        """Test failed toolchain fetch"""
        mock_ssh.side_effect = machine.MachineError('fetch failed')
        m = machine.Machine('host1')
        self.assertFalse(m.fetch_toolchain('buildman', 'arm'))

    @mock.patch('buildman.machine._run_ssh')
    def test_weight_unavailable(self, mock_ssh):
        """Test weight is 0 when unavailable"""
        m = machine.Machine('host1')
        m.avail = False
        m._calc_weight()
        self.assertEqual(m.weight, 0)


class TestRunSshExtended(unittest.TestCase):
    """Extended _run_ssh tests"""

    @mock.patch('buildman.machine.command.run_pipe')
    def test_failure_no_stderr(self, mock_pipe):
        """Test SSH failure with no stderr"""
        mock_pipe.return_value = mock.Mock(
            return_code=1, stdout='', stderr='')
        with self.assertRaises(machine.MachineError) as ctx:
            machine._run_ssh('host1', ['cmd'])
        self.assertIn('failed with code 1', str(ctx.exception))

    @mock.patch('buildman.machine.command.run_pipe')
    def test_stdin_data(self, mock_pipe):
        """Test SSH with stdin_data"""
        mock_pipe.return_value = mock.Mock(
            return_code=0, stdout='result\n', stderr='')
        result = machine._run_ssh('host1', ['python3'],
                                  stdin_data='print("result")')
        self.assertEqual(result, 'result\n')
        # Verify stdin_data was passed through
        _, kwargs = mock_pipe.call_args
        self.assertEqual(kwargs['stdin_data'], 'print("result")')


class TestMachinePoolExtended(unittest.TestCase):
    """Extended MachinePool tests for coverage"""

    def setUp(self):
        bsettings.setup(None)

    def test_load_with_max_boards(self):
        """Test loading machines with max_boards config"""
        bsettings.add_file(
            '[machines]\n'
            'server1\n'
            '[machine:server1]\n'
            'max_boards = 50\n'
        )
        pool = machine.MachinePool()
        self.assertEqual(len(pool.machines), 1)
        self.assertEqual(pool.machines[0].max_boards, 50)

    def test_load_filtered_names(self):
        """Test loading only specified machine names"""
        bsettings.add_file(
            '[machines]\n'
            'host1\n'
            'host2\n'
            'host3\n'
        )
        pool = machine.MachinePool(names=['host1', 'host3'])
        self.assertEqual(len(pool.machines), 2)
        names = [m.hostname for m in pool.machines]
        self.assertEqual(names, ['host1', 'host3'])

    def test_get_reachable(self):
        """Test get_reachable includes busy machines"""
        bsettings.add_file('[machines]\nhost1\nhost2\n')
        pool = machine.MachinePool()
        # Simulate host1 reachable but busy, host2 unreachable
        pool.machines[0].avail = False
        pool.machines[0].info.arch = 'x86_64'
        self.assertEqual(len(pool.get_reachable()), 1)
        self.assertEqual(pool.get_reachable()[0].hostname, 'host1')

    @mock.patch('buildman.machine._run_ssh')
    def test_print_summary(self, mock_ssh):
        """Test print_summary runs without error"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'load_1m': 1.0,
            'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        pool.probe_all()
        # Just verify it doesn't crash
        with terminal.capture():
            pool.print_summary()

    @mock.patch('buildman.machine._run_ssh')
    def test_print_summary_with_toolchains(self, mock_ssh):
        """Test print_summary with toolchain info"""
        def ssh_side_effect(_hostname, cmd, **_kwargs):
            if 'python3' in cmd:
                return json.dumps({
                    **MACHINE_INFO, 'load_1m': 1.0,
                    'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
            if '--list-tool-chains' in cmd:
                return 'List of available toolchains (1):\narm : /bin/gcc\n\n'
            return ''

        mock_ssh.side_effect = ssh_side_effect
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        pool.probe_all()
        pool.check_toolchains({'arm', 'sandbox'})
        with terminal.capture():
            pool.print_summary(local_archs={'arm', 'sandbox'})

    @mock.patch('buildman.machine._run_ssh')
    def test_check_toolchains_version_mismatch(self, mock_ssh):
        """Test version mismatch detection in check_toolchains"""
        home = os.path.expanduser('~')

        # The remote has gcc-12, but the boss has gcc-13
        remote_gcc = (f'.buildman-toolchains/gcc-12.0.0-nolibc/'
                      'arm/bin/arm-linux-gcc')

        def ssh_side_effect(_hostname, cmd, **_kwargs):
            if 'python3' in cmd:
                return json.dumps({
                    **MACHINE_INFO, 'load_1m': 1.0,
                    'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
            # Boss probe: report arm as present (wrong version)
            return 'arm:yes\n'

        mock_ssh.side_effect = ssh_side_effect
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        pool.probe_all()

        local_gcc = {
            'arm': f'{home}/.buildman-toolchains/gcc-13.1.0-nolibc/'
                   'arm/bin/arm-linux-gcc',
        }
        # check_toolchains will re-probe, which sets the remote path
        # via _probe_toolchains_from_boss. The SSH mock returns
        # arm:yes, so the remote path uses the boss's relative path
        # but we need it to have the old version. Patch the machine's
        # toolchains after the probe runs.
        orig_probe = machine.Machine._probe_toolchains_from_boss

        def fake_probe(self_mach, lg):
            orig_probe(self_mach, lg)
            # Override with the wrong version
            if 'arm' in self_mach.toolchains:
                self_mach.toolchains['arm'] = f'~/{remote_gcc}'
            return self_mach.toolchains

        with mock.patch.object(machine.Machine,
                               '_probe_toolchains_from_boss',
                               fake_probe):
            missing = pool.check_toolchains({'arm'}, local_gcc=local_gcc)

        # arm should be flagged as missing due to version mismatch
        self.assertEqual(len(missing), 1)


class TestFetchMissing(unittest.TestCase):
    """Test _fetch_all_missing()"""

    def setUp(self):
        bsettings.setup(None)

    @mock.patch('buildman.machine._run_ssh')
    def test_fetch_success(self, mock_ssh):
        """Test successful toolchain fetch"""
        mock_ssh.return_value = ''
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        m = pool.machines[0]
        m.avail = True
        m.info.arch = 'x86_64'

        missing_map = {m: {'arm'}}
        with terminal.capture():
            pool._fetch_all_missing(missing_map, {}, None, 'buildman')
        # After successful fetch + re-probe, arch is removed
        # (re-probe returns empty since SSH mock returns '')

    @mock.patch('buildman.machine._run_ssh')
    def test_fetch_failure(self, mock_ssh):
        """Test failed toolchain fetch"""
        mock_ssh.side_effect = machine.MachineError('failed')
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        m = pool.machines[0]
        m.avail = True
        m.info.arch = 'x86_64'

        missing_map = {m: {'arm'}}
        with terminal.capture():
            pool._fetch_all_missing(missing_map, {}, None, 'buildman')
        # Should still have the missing arch
        self.assertIn('arm', missing_map[m])

    @mock.patch('buildman.machine._run_ssh')
    def test_fetch_with_version_removal(self, mock_ssh):
        """Test fetching removes old version first"""
        calls = []

        def ssh_side_effect(hostname, cmd, **_kwargs):
            calls.append(cmd)
            if '--fetch-arch' in cmd:
                return ''
            if 'rm' in cmd:
                return ''
            return ''

        mock_ssh.side_effect = ssh_side_effect
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        m = pool.machines[0]
        m.avail = True
        m.info.arch = 'x86_64'
        m.toolchains = {
            'arm': '~/.buildman-toolchains/gcc-12.0.0-nolibc/arm/bin/gcc'}

        missing_map = {m: {'arm'}}
        local_versions = {'arm': 'gcc-13.1.0-nolibc'}
        with terminal.capture():
            pool._fetch_all_missing(missing_map, local_versions, None,
                                    'buildman')
        # Should have called rm -rf for the old version
        rm_calls = [c for c in calls if 'rm' in c]
        self.assertTrue(len(rm_calls) > 0)


class TestPrintSummaryEdgeCases(unittest.TestCase):
    """Test print_summary edge cases"""

    def setUp(self):
        bsettings.setup(None)

    @mock.patch('buildman.machine._run_ssh')
    def test_unavailable_machine(self, mock_ssh):
        """Test summary with unavailable machine"""
        mock_ssh.side_effect = machine.MachineError('refused')
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        pool.probe_all()
        # Should not crash with unavailable machine
        with terminal.capture():
            pool.print_summary()

    @mock.patch('buildman.machine._run_ssh')
    def test_busy_machine(self, mock_ssh):
        """Test summary with busy machine"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'load_1m': 10.0})
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        pool.probe_all()
        with terminal.capture():
            pool.print_summary()

    @mock.patch('buildman.machine._run_ssh')
    def test_with_max_boards(self, mock_ssh):
        """Test summary shows max_boards"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'load_1m': 1.0,
            'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
        bsettings.add_file(
            '[machines]\nhost1\n'
            '[machine:host1]\nmax_boards = 50\n')
        pool = machine.MachinePool()
        pool.probe_all()
        with terminal.capture():
            pool.print_summary()

    @mock.patch('buildman.machine._run_ssh')
    def test_with_tc_error(self, mock_ssh):
        """Test summary with toolchain error note"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'load_1m': 1.0,
            'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        pool.probe_all()
        pool.machines[0].tc_error = 'buildman not found'
        with terminal.capture():
            pool.print_summary(local_archs={'arm'})

    @mock.patch('buildman.machine._run_ssh')
    def test_with_missing_toolchains(self, mock_ssh):
        """Test summary with missing toolchain notes"""
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'load_1m': 1.0,
            'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        pool.probe_all()
        pool.machines[0].toolchains = {'sandbox': '/usr/bin/gcc'}
        local_gcc = {
            'arm': os.path.expanduser(
                '~/.buildman-toolchains/gcc-13/arm/bin/gcc'),
        }
        with terminal.capture():
            pool.print_summary(local_archs={'arm', 'sandbox'},
                               local_gcc=local_gcc)


class TestCheckToolchainsEdge(unittest.TestCase):
    """Test check_toolchains edge cases"""

    def setUp(self):
        bsettings.setup(None)

    def test_no_reachable(self):
        """Test check_toolchains with no reachable machines (line 482)"""
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        # Machine is not probed, so not reachable
        result = pool.check_toolchains({'arm'})
        self.assertEqual(result, {})

    @mock.patch('buildman.machine._run_ssh')
    def test_fetch_flag(self, mock_ssh):
        """Test check_toolchains with fetch=True (line 524)"""
        home = os.path.expanduser('~')

        def ssh_side_effect(_hostname, cmd, **_kwargs):
            if 'python3' in cmd:
                return json.dumps({
                    **MACHINE_INFO, 'load_1m': 1.0,
                    'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
            # No toolchains found
            return ''

        mock_ssh.side_effect = ssh_side_effect
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        pool.probe_all()

        local_gcc = {
            'arm': f'{home}/.buildman-toolchains/gcc-13/arm/bin/gcc',
        }
        # fetch=True should trigger _fetch_all_missing
        with mock.patch.object(pool, '_fetch_all_missing') as mock_fetch:
            pool.check_toolchains({'arm'}, fetch=True,
                                  local_gcc=local_gcc)
            mock_fetch.assert_called_once()


class TestFetchVersionRemovalFailure(unittest.TestCase):
    """Test _fetch_all_missing rm -rf failure path (lines 563-564)"""

    def setUp(self):
        bsettings.setup(None)

    @mock.patch('buildman.machine._run_ssh')
    def test_rm_failure_continues(self, mock_ssh):
        """Test that rm -rf failure is silently ignored"""
        call_count = [0]

        def ssh_side_effect(hostname, cmd, **_kwargs):
            call_count[0] += 1
            if 'rm' in cmd:
                raise machine.MachineError('rm failed')
            if '--fetch-arch' in cmd:
                return ''
            return ''

        mock_ssh.side_effect = ssh_side_effect
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        m = pool.machines[0]
        m.avail = True
        m.info.arch = 'x86_64'
        # Old version that differs from local
        m.toolchains = {
            'arm': '~/.buildman-toolchains/gcc-12.0.0-nolibc/arm/bin/gcc'}

        missing_map = {m: {'arm'}}
        local_versions = {'arm': 'gcc-13.1.0-nolibc'}
        with terminal.capture():
            pool._fetch_all_missing(missing_map, local_versions, None,
                                    'buildman')
        # Should have attempted rm and then fetch despite rm failure
        self.assertGreater(call_count[0], 1)


class TestPrintSummaryNotProbed(unittest.TestCase):
    """Test print_summary 'not probed' branch (lines 669-670)"""

    def setUp(self):
        bsettings.setup(None)

    def test_not_probed_machine(self):
        """Test summary shows 'not probed' for unprobed machine"""
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        # Don't probe - machine stays in 'not probed' state
        with terminal.capture():
            pool.print_summary()


class TestPrintSummaryMissingNoVersion(unittest.TestCase):
    """Test print_summary missing toolchain without version (line 707)"""

    def setUp(self):
        bsettings.setup(None)

    @mock.patch('buildman.machine._run_ssh')
    def test_missing_with_and_without_version(self, mock_ssh):
        """Test missing note for archs with and without version info"""
        home = os.path.expanduser('~')
        mock_ssh.return_value = json.dumps({
            **MACHINE_INFO, 'load_1m': 1.0,
            'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
        bsettings.add_file('[machines]\nhost1\n')
        pool = machine.MachinePool()
        pool.probe_all()
        pool.machines[0].toolchains = {}
        # arm has a version (under ~/.buildman-toolchains),
        # sandbox does not
        local_gcc = {
            'arm': f'{home}/.buildman-toolchains/gcc-13.1.0-nolibc/'
                   'arm/bin/gcc',
            'sandbox': '/usr/bin/gcc',
        }
        with terminal.capture():
            pool.print_summary(local_archs={'arm', 'sandbox'},
                               local_gcc=local_gcc)


class TestDoProbe(unittest.TestCase):
    """Test do_probe_machines()"""

    def setUp(self):
        bsettings.setup(None)

    def test_no_machines(self):
        """Test with no machines configured"""
        with terminal.capture():
            ret = machine.do_probe_machines()
        self.assertEqual(ret, 1)

    @mock.patch('buildman.machine._run_ssh')
    def test_with_machines(self, mock_ssh):
        """Test probing configured machines"""
        def ssh_side_effect(_hostname, cmd, **_kwargs):
            if 'python3' in cmd:
                return json.dumps({
                    **MACHINE_INFO, 'load_1m': 1.0,
                    'mem_avail_mb': 8000, 'disk_avail_mb': 20000})
            if '--list-tool-chains' in cmd:
                return 'List of available toolchains (0):\n\n'
            return ''

        mock_ssh.side_effect = ssh_side_effect
        bsettings.add_file('[machines]\nhost1\n')
        with terminal.capture():
            ret = machine.do_probe_machines()
        self.assertEqual(ret, 0)


if __name__ == '__main__':
    unittest.main()
