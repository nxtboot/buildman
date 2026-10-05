Changelog
=========

All notable changes to this project are documented here. The format is
based on `Keep a Changelog <https://keepachangelog.com/en/1.1.0/>`_ and
the project follows `Semantic Versioning <https://semver.org/>`_.

Unreleased
----------

0.1.0 - 2026-10-05
------------------

Changed
~~~~~~~
- Buildman is now developed in its own repository at
  https://github.com/nxtboot/buildman, with its history carried over from
  the U-Boot tree.
- Buildman is distributed as the self-contained ``buildman`` package, with
  ``u_boot_pylib``, the patman modules it uses and U-Boot's ``qconfig`` tool
  vendored in, installable from PyPI with ``pip install buildman``. It no
  longer depends on the ``u_boot_pylib`` and ``patch-manager`` packages.
- For distributed builds, the boss copies just the buildman package to each
  worker, since this now includes everything the worker needs.
- Documentation is now published at https://buildman.readthedocs.io/.

Earlier releases
----------------

Releases 0.0.7 and earlier were published from the U-Boot tree and predate
this changelog.
