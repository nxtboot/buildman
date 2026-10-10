Contributing to Buildman
========================

Development setup
-----------------

Work in a virtual environment::

    python -m venv .venv
    . .venv/bin/activate
    pip install -r requirements.txt
    pip install -e .[test]

Running the tests
-----------------

::

    buildman -t                 # the whole suite
    buildman -t <name>          # a single test, e.g. test_regen_boards
    buildman --coverage         # check that the tests cover all the code

Most tests use fake toolchains and a fake source tree, so no U-Boot source
or cross-compilers are needed. A few tests run U-Boot's real Kconfig tools
(``merge_config.sh`` and ``make savedefconfig``). To include them, point
``UBOOT_SRC`` at a U-Boot source tree, which needs a host compiler along with
``bison`` and ``flex``::

    UBOOT_SRC=/path/to/u-boot buildman -t

Otherwise they are skipped. The tree must be recent enough to have the
``BUILDMAN_TEST_A`` option in ``test/Kconfig``. The CI uses the U-Boot commit
given by ``UBOOT_REF`` in ``.github/workflows/test.yml``.

Building the documentation
--------------------------

::

    pip install -r doc/requirements.txt
    make -C doc html        # output in doc/_build/html

The manual itself lives in ``buildman/buildman.rst``; ``doc/`` only wraps it
for Sphinx, so edit the manual there.

Building the package
--------------------

::

    python -m build
    twine check --strict dist/*

Vendored code
-------------

Some code is copied from the U-Boot tree, so that the project is
self-contained:

- ``buildman/_vendor/patman/``: the patman modules which buildman uses to
  read series metadata (``patchstream``, ``commit`` and their dependencies)
- ``buildman/_vendor/qconfig.py``: U-Boot's tool for querying CONFIG options

The vendored code lives inside the ``buildman`` package, rather than at the
top level, so that installing buildman does not clash with U-Boot's own
copies or with other packages which provide them. ``buildman/__init__.py``
adds ``buildman/_vendor/`` to the start of the import path, so the code still
imports them by their usual names and matches the U-Boot tree. It also means
distributed builds can copy the package to workers along with the
``u_boot_pylib`` library, which is not vendored: it is a dependency, from the
``u-boot-pylib`` package (https://github.com/nxtboot/u-boot-pylib).

When refreshing these from U-Boot, note the U-Boot commit in the commit
message.

Continuous integration
----------------------

The *Tests* workflow runs the suite on Python 3.10-3.12, checks test coverage
and builds and checks the package on every push and pull request.

Making a release
----------------

Releases are published to PyPI automatically by the *Release* workflow
when a version tag is pushed. The flow is:

1. Update ``CHANGELOG.rst``: move the ``Unreleased`` entries under a new
   ``X.Y.Z - <date>`` heading.
2. Bump ``version`` in ``pyproject.toml``.
3. Commit the changes.
4. Tag it. The tag must be ``v`` followed by the exact version, for
   example::

       git tag v0.1.0
       git push origin v0.1.0

   A tag containing ``rc`` (for example ``v0.1.0rc1``) publishes to
   TestPyPI; a final tag publishes to the real PyPI. The workflow
   refuses to publish if the tag does not match the project version.

   You can also trigger the *Release* workflow manually (the
   ``workflow_dispatch`` option) to publish the current branch to
   TestPyPI for a dry run, without a tag.

Publishing uses PyPI Trusted Publishing (OIDC), so no API tokens are
stored. The PyPI and TestPyPI projects must each be configured to trust
this repository's ``release.yml`` workflow (environments ``pypi`` and
``testpypi``) before the first release.
