# SPDX-License-Identifier: GPL-2.0+
"""Sphinx configuration for the buildman documentation."""

import pathlib
import tomllib

_docdir = pathlib.Path(__file__).resolve().parent
_pyproject = _docdir.parent / 'pyproject.toml'
with open(_pyproject, 'rb') as _fd:
    _meta = tomllib.load(_fd)['project']

project = 'Buildman'
author = 'Simon Glass and contributors'
copyright = '2013-2026, Simon Glass and contributors'
release = _meta['version']
version = release

extensions = [
    'sphinx.ext.autosectionlabel',
    'sphinx.ext.intersphinx',
]

# Section labels are referenced as 'document:Section title'
autosectionlabel_prefix_document = True

# Resolve references into the U-Boot documentation, which buildman's manual
# refers to in places
intersphinx_mapping = {
    'uboot': ('https://docs.u-boot-project.org/en/latest/', None),
}
templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

html_theme = 'sphinx_rtd_theme'
html_static_path = []

# Keep the table of contents fully expanded in the sidebar, so clicking
# a section does not collapse the rest of the navigation.
html_theme_options = {
    'collapse_navigation': False,
    'sticky_navigation': True,
    'navigation_depth': 4,
}
