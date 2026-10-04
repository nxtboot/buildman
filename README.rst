Buildman build tool
===================

.. image:: https://github.com/nxtboot/buildman/actions/workflows/test.yml/badge.svg
   :target: https://github.com/nxtboot/buildman/actions/workflows/test.yml
   :alt: Test status

.. image:: https://img.shields.io/pypi/v/buildman.svg
   :target: https://pypi.org/project/buildman/
   :alt: PyPI version

.. image:: https://readthedocs.org/projects/buildman/badge/?version=latest
   :target: https://buildman.readthedocs.io/en/latest/
   :alt: Documentation status

.. image:: https://img.shields.io/pypi/pyversions/buildman.svg
   :target: https://pypi.org/project/buildman/
   :alt: Supported Python versions

Buildman builds U-Boot, for many boards and many commits at once, to check
that a patch series has not broken anything. It:

- builds each commit of a branch for any set of boards, in parallel, making
  full use of multi-processor machines;
- summarises the results, so that new errors, warnings or image-size changes
  can be pinned to the commit and board which caused them;
- fetches the cross-compilers it needs and works out which to use for each
  board;
- can share builds across several machines over SSH;
- adjusts board configurations on the fly for build experiments.

Installation
------------

Install the latest release from PyPI::

    pip install buildman

The ``buildman`` command is then on your path.

Quick start
-----------

From a U-Boot source tree, fetch a toolchain and build a board::

    buildman --fetch-arch arm
    buildman -k rpi_2
    ls ../current/rpi_2

Build every commit of the current branch for all ARM boards, then show a
summary of the results::

    buildman -b <branch> arm
    buildman -b <branch> -s arm

See ``buildman -H`` for the complete manual, or the documentation linked
below.

Documentation
-------------

Full documentation is hosted on Read the Docs:

    https://buildman.readthedocs.io/

Development
-----------

Run the test suite from a checkout::

    pip install -r requirements.txt
    pip install -e .[test]
    buildman -t

A few tests run U-Boot's real Kconfig tools. Set ``UBOOT_SRC`` to a U-Boot
source tree to include them; otherwise they are skipped. ``buildman
--coverage`` checks that the tests cover all of the code.

The ``u_boot_pylib`` library, the parts of patman which buildman uses and
U-Boot's ``qconfig`` tool are vendored from the U-Boot tree, so no surrounding
U-Boot source is needed to run the tests.
