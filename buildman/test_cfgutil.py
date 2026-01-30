# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2022 Google LLC
#

"""Tests for cfgutil module"""

import os
import tempfile
import unittest

from buildman import cfgutil


class TestAdjustCfg(unittest.TestCase):
    """Tests for config adjustment functions"""

    def test_adjust_cfg_nop(self):
        """check various adjustments of config that are nops"""
        # enable an enabled CONFIG
        self.assertEqual(
            'CONFIG_FRED=y',
            cfgutil.adjust_cfg_line('CONFIG_FRED=y', {'FRED':'FRED'})[0])

        # disable a disabled CONFIG
        self.assertEqual(
            '# CONFIG_FRED is not set',
            cfgutil.adjust_cfg_line(
                '# CONFIG_FRED is not set', {'FRED':'~FRED'})[0])

        # use the adjust_cfg_lines() function
        self.assertEqual(
            ['CONFIG_FRED=y'],
            cfgutil.adjust_cfg_lines(['CONFIG_FRED=y'], {'FRED':'FRED'}))
        self.assertEqual(
            ['# CONFIG_FRED is not set'],
            cfgutil.adjust_cfg_lines(['CONFIG_FRED=y'], {'FRED':'~FRED'}))

        # handling an empty line
        self.assertEqual('#', cfgutil.adjust_cfg_line('#', {'FRED':'~FRED'})[0])

    def test_adjust_cfg(self):
        """check various adjustments of config"""
        # disable a CONFIG
        self.assertEqual(
            '# CONFIG_FRED is not set',
            cfgutil.adjust_cfg_line('CONFIG_FRED=1' , {'FRED':'~FRED'})[0])

        # enable a disabled CONFIG
        self.assertEqual(
            'CONFIG_FRED=y',
            cfgutil.adjust_cfg_line(
                '# CONFIG_FRED is not set', {'FRED':'FRED'})[0])

        # enable a CONFIG that doesn't exist
        self.assertEqual(
            ['CONFIG_FRED=y'],
            cfgutil.adjust_cfg_lines([], {'FRED':'FRED'}))

        # disable a CONFIG that doesn't exist
        self.assertEqual(
            ['# CONFIG_FRED is not set'],
            cfgutil.adjust_cfg_lines([], {'FRED':'~FRED'}))

        # disable a value CONFIG
        self.assertEqual(
            '# CONFIG_FRED is not set',
            cfgutil.adjust_cfg_line('CONFIG_FRED="fred"' , {'FRED':'~FRED'})[0])

        # setting a value CONFIG
        self.assertEqual(
            'CONFIG_FRED="fred"',
            cfgutil.adjust_cfg_line('# CONFIG_FRED is not set' ,
                                    {'FRED':'FRED="fred"'})[0])

        # changing a value CONFIG
        self.assertEqual(
            'CONFIG_FRED="fred"',
            cfgutil.adjust_cfg_line('CONFIG_FRED="ernie"' ,
                                    {'FRED':'FRED="fred"'})[0])

        # setting a value for a CONFIG that doesn't exist
        self.assertEqual(
            ['CONFIG_FRED="fred"'],
            cfgutil.adjust_cfg_lines([], {'FRED':'FRED="fred"'}))

    def test_convert_adjust_cfg_list(self):
        """Check conversion of the list of changes into a dict"""
        self.assertEqual({}, cfgutil.convert_list_to_dict(None))

        expect = {
            'FRED':'FRED',
            'MARY':'~MARY',
            'JOHN':'JOHN=0x123',
            'ALICE':'ALICE="alice"',
            'AMY':'AMY',
            'ABE':'~ABE',
            'MARK':'MARK=0x456',
            'ANNA':'ANNA="anna"',
            }
        actual = cfgutil.convert_list_to_dict(
            ['FRED', '~MARY', 'JOHN=0x123', 'ALICE="alice"',
             'CONFIG_AMY', '~CONFIG_ABE', 'CONFIG_MARK=0x456',
             'CONFIG_ANNA="anna"'])
        self.assertEqual(expect, actual)

        # Test comma-separated values
        actual = cfgutil.convert_list_to_dict(
            ['FRED,~MARY,JOHN=0x123', 'ALICE="alice"',
             'CONFIG_AMY,~CONFIG_ABE', 'CONFIG_MARK=0x456,CONFIG_ANNA="anna"'])
        self.assertEqual(expect, actual)

        # Test mixed comma-separated and individual values
        actual = cfgutil.convert_list_to_dict(
            ['FRED,~MARY', 'JOHN=0x123', 'ALICE="alice",CONFIG_AMY',
             '~CONFIG_ABE,CONFIG_MARK=0x456', 'CONFIG_ANNA="anna"'])
        self.assertEqual(expect, actual)

    def test_adjust_cfg_to_fragment(self):
        """Test adjust_cfg_to_fragment creates correct fragment content"""
        # Empty dict returns empty string
        self.assertEqual('', cfgutil.adjust_cfg_to_fragment({}))

        # Enable option
        self.assertEqual('CONFIG_FRED=y\n',
                         cfgutil.adjust_cfg_to_fragment({'FRED': 'FRED'}))

        # Disable option
        self.assertEqual('# CONFIG_FRED is not set\n',
                         cfgutil.adjust_cfg_to_fragment({'FRED': '~FRED'}))

        # Set value
        self.assertEqual('CONFIG_FRED=0x123\n',
                         cfgutil.adjust_cfg_to_fragment({'FRED': 'FRED=0x123'}))

        # Set string value
        self.assertEqual('CONFIG_FRED="fred"\n',
                         cfgutil.adjust_cfg_to_fragment({'FRED': 'FRED="fred"'}))

        # Multiple options (note: dict order is preserved in Python 3.7+)
        result = cfgutil.adjust_cfg_to_fragment({
            'FRED': 'FRED',
            'MARY': '~MARY',
            'JOHN': 'JOHN=42'
        })
        self.assertIn('CONFIG_FRED=y', result)
        self.assertIn('# CONFIG_MARY is not set', result)
        self.assertIn('CONFIG_JOHN=42', result)

    def test_check_cfg_file(self):
        """Test check_cfg_file detects conflicts as expected"""
        # Check failure to disable CONFIG
        result = cfgutil.check_cfg_lines(['CONFIG_FRED=1'], {'FRED':'~FRED'})
        self.assertEqual([['~FRED', 'CONFIG_FRED=1']], result)

        result = cfgutil.check_cfg_lines(
            ['CONFIG_FRED=1', 'CONFIG_MARY="mary"'], {'FRED':'~FRED'})
        self.assertEqual([['~FRED', 'CONFIG_FRED=1']], result)

        result = cfgutil.check_cfg_lines(
            ['CONFIG_FRED=1', 'CONFIG_MARY="mary"'], {'MARY':'~MARY'})
        self.assertEqual([['~MARY', 'CONFIG_MARY="mary"']], result)

        # Check failure to enable CONFIG
        result = cfgutil.check_cfg_lines(
            ['# CONFIG_FRED is not set'], {'FRED':'FRED'})
        self.assertEqual([['FRED', '# CONFIG_FRED is not set']], result)

        # Check failure to set CONFIG value
        result = cfgutil.check_cfg_lines(
            ['# CONFIG_FRED is not set', 'CONFIG_MARY="not"'],
            {'MARY':'MARY="mary"', 'FRED':'FRED'})
        self.assertEqual([
            ['FRED', '# CONFIG_FRED is not set'],
            ['MARY="mary"', 'CONFIG_MARY="not"']], result)

        # Check failure to add CONFIG value
        result = cfgutil.check_cfg_lines([], {'MARY':'MARY="mary"'})
        self.assertEqual([
            ['MARY="mary"', 'Missing expected line: CONFIG_MARY="mary"']],
            result)


class TestRunMergeConfig(unittest.TestCase):
    """Tests for run_merge_config() function"""

    def test_merge_script_path(self):
        """Test that merge_config.sh path is relative to cwd, not absolute"""
        from unittest import mock
        from u_boot_pylib import command

        # Track commands that were run
        commands_run = []

        def mock_run_one(*args, **kwargs):
            commands_run.append((args, kwargs))
            result = command.CommandResult()
            result.return_code = 0
            result.stdout = ''
            result.stderr = ''
            return result

        with mock.patch.object(command, 'run_one', mock_run_one):
            with mock.patch('os.path.exists', return_value=True):
                with mock.patch('os.unlink'):
                    # Use a work directory path like buildman does
                    src_dir = '../branch/.bm-work/00'
                    cfgutil.run_merge_config(
                        src_dir, 'build', 'build/.config',
                        {'LOCALVERSION_AUTO': '~LOCALVERSION_AUTO'}, {})

        # Find the merge_config.sh command
        merge_cmd = None
        for args, kwargs in commands_run:
            if args and 'merge_config.sh' in args[0]:
                merge_cmd = args
                merge_cwd = kwargs.get('cwd')
                break

        self.assertIsNotNone(merge_cmd, 'merge_config.sh command not found')

        # The script path should be relative, not include src_dir
        script_path = merge_cmd[0]
        self.assertEqual('scripts/kconfig/merge_config.sh', script_path,
                         f'Script path should be relative, got: {script_path}')

        # The cwd should be src_dir
        self.assertEqual(src_dir, merge_cwd,
                         f'cwd should be src_dir, got: {merge_cwd}')


class TestProcessConfig(unittest.TestCase):
    """Tests for process_config() function"""

    def test_process_config_defconfig(self):
        """Test process_config() with .config style file"""
        config_data = '''# This is a comment
CONFIG_OPTION_A=y
CONFIG_OPTION_B="string"
CONFIG_OPTION_C=123
# CONFIG_OPTION_D is not set
'''
        with tempfile.NamedTemporaryFile(mode='w', delete=False,
                                         suffix='.config') as tmp:
            tmp.write(config_data)
            tmp_name = tmp.name

        try:
            config = cfgutil.process_config(tmp_name, squash_config_y=False)

            self.assertEqual('y', config['CONFIG_OPTION_A'])
            self.assertEqual('"string"', config['CONFIG_OPTION_B'])
            self.assertEqual('123', config['CONFIG_OPTION_C'])
            # Comments should be ignored
            self.assertNotIn('CONFIG_OPTION_D', config)
        finally:
            os.unlink(tmp_name)

    def test_process_config_autoconf_h(self):
        """Test process_config() with autoconf.h style file"""
        config_data = '''/* Auto-generated header */
#define CONFIG_OPTION_A 1
#define CONFIG_OPTION_B "value"
#define CONFIG_OPTION_C
#define NOT_CONFIG 1
'''
        with tempfile.NamedTemporaryFile(mode='w', delete=False,
                                         suffix='.h') as tmp:
            tmp.write(config_data)
            tmp_name = tmp.name

        try:
            config = cfgutil.process_config(tmp_name, squash_config_y=False)

            self.assertEqual('1', config['CONFIG_OPTION_A'])
            self.assertEqual('"value"', config['CONFIG_OPTION_B'])
            # #define without value gets empty string (squash_config_y=False)
            self.assertEqual('', config['CONFIG_OPTION_C'])
            # Non-CONFIG_ defines should be ignored
            self.assertNotIn('NOT_CONFIG', config)
        finally:
            os.unlink(tmp_name)

    def test_process_config_nonexistent(self):
        """Test process_config() with non-existent file"""
        config = cfgutil.process_config('/nonexistent/path/config',
                                        squash_config_y=False)
        self.assertEqual({}, config)

    def test_process_config_squash_y(self):
        """Test process_config() with squash_config_y enabled"""
        config_data = '''CONFIG_OPTION_A=y
CONFIG_OPTION_B=n
#define CONFIG_OPTION_C
'''
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
            tmp.write(config_data)
            tmp_name = tmp.name

        try:
            config = cfgutil.process_config(tmp_name, squash_config_y=True)

            # y should be squashed to 1
            self.assertEqual('1', config['CONFIG_OPTION_A'])
            # n should remain n
            self.assertEqual('n', config['CONFIG_OPTION_B'])
            # Empty #define should get '1' when squash_config_y is True
            self.assertEqual('1', config['CONFIG_OPTION_C'])
        finally:
            os.unlink(tmp_name)


if __name__ == "__main__":
    unittest.main()
