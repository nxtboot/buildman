# SPDX-License-Identifier: GPL-2.0+
# Copyright (c) 2024 Google, Inc

"""Tests for bsettings.py"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from buildman import bsettings
from u_boot_pylib import tools


class TestBsettings(unittest.TestCase):
    """Test bsettings module"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmpdir)

    def test_setup_no_file(self):
        """Test setup() with fname=None (no config file)"""
        bsettings.setup(None)
        # Should not raise, settings should be empty
        self.assertEqual([], bsettings.get_items('nonexistent'))

    def test_setup_default_missing(self):
        """Test setup() creates config when default file missing"""
        # Use a non-existent path for HOME
        fake_home = os.path.join(self._tmpdir, 'fakehome')
        os.makedirs(fake_home)
        config_file = os.path.join(fake_home, '.buildman')

        with mock.patch.dict(os.environ, {'HOME': fake_home}):
            with mock.patch('builtins.print'):
                bsettings.setup('')

        # Config file should have been created
        self.assertTrue(os.path.exists(config_file))

    def test_setup_existing_file(self):
        """Test setup() reads existing config file"""
        config_file = os.path.join(self._tmpdir, 'test.buildman')
        tools.write_file(config_file, '[toolchain]\narm = /opt/arm\n',
                         binary=False)

        bsettings.setup(config_file)
        items = bsettings.get_items('toolchain')
        self.assertEqual([('arm', '/opt/arm')], items)

    def test_add_file(self):
        """Test add_file() adds config data"""
        bsettings.setup(None)
        bsettings.add_file('[test]\nkey = value\n')
        items = bsettings.get_items('test')
        self.assertEqual([('key', 'value')], items)

    def test_add_section(self):
        """Test add_section() creates new section"""
        bsettings.setup(None)
        bsettings.add_section('newsection')
        # Section should exist but be empty
        self.assertEqual([], bsettings.get_items('newsection'))

    def test_get_items_missing_section(self):
        """Test get_items() returns empty list for missing section"""
        bsettings.setup(None)
        self.assertEqual([], bsettings.get_items('nonexistent'))

    def test_get_items_other_error(self):
        """Test get_items() re-raises non-NoSectionError exceptions"""
        bsettings.setup(None)
        with mock.patch.object(bsettings.settings, 'items',
                               side_effect=ValueError('test error')):
            with self.assertRaises(ValueError):
                bsettings.get_items('test')

    def test_get_global_item_value(self):
        """Test get_global_item_value() retrieves global items"""
        bsettings.setup(None)
        bsettings.add_file('[global]\nmykey = myvalue\n')
        self.assertEqual('myvalue', bsettings.get_global_item_value('mykey'))
        self.assertIsNone(bsettings.get_global_item_value('missing'))

    def test_set_item(self):
        """Test set_item() sets value and writes to file"""
        config_file = os.path.join(self._tmpdir, 'test_set.buildman')
        tools.write_file(config_file, '[toolchain]\n', binary=False)

        bsettings.setup(config_file)
        bsettings.set_item('toolchain', 'newkey', 'newvalue')

        # Value should be set in memory
        items = dict(bsettings.get_items('toolchain'))
        self.assertEqual('newvalue', items['newkey'])

        # Value should be written to file
        content = tools.read_file(config_file, binary=False)
        self.assertIn('newkey', content)
        self.assertIn('newvalue', content)

    def test_set_item_no_file(self):
        """Test set_item() when config_fname is None"""
        # Explicitly reset config_fname to None
        bsettings.config_fname = None
        bsettings.setup(None)
        bsettings.add_section('test')
        # Should not raise even though there's no file to write
        bsettings.set_item('test', 'key', 'value')
        items = dict(bsettings.get_items('test'))
        self.assertEqual('value', items['key'])

    def test_create_buildman_config_file(self):
        """Test create_buildman_config_file() creates valid config"""
        config_file = os.path.join(self._tmpdir, 'new.buildman')

        bsettings.create_buildman_config_file(config_file)

        self.assertTrue(os.path.exists(config_file))
        content = tools.read_file(config_file, binary=False)
        self.assertIn('[toolchain]', content)
        self.assertIn('[toolchain-prefix]', content)
        self.assertIn('[toolchain-alias]', content)
        self.assertIn('[make-flags]', content)

    def test_create_buildman_config_file_error(self):
        """Test create_buildman_config_file() handles IOError"""
        # Try to create file in non-existent directory
        bad_path = '/nonexistent/path/config'

        with mock.patch('builtins.print'):
            with self.assertRaises(IOError):
                bsettings.create_buildman_config_file(bad_path)


if __name__ == '__main__':
    unittest.main()
