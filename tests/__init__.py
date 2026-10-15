"""Test package marker.

`python -m unittest discover -s tests` and the `python tools/runtests.py` runner both import
the modules as `tests.test_app` etc., which needs this file to be a regular
package rather than an implicit namespace package: a namespace package resolves
by `sys.path` order, so two checkouts on the same path would silently mix.

`python -m` is the supported invocation for the suite; running a test file
directly (`python tests\\test_app.py`) puts `tests\\` on `sys.path` instead of the
project root, and `import main` / `import translators` then resolve to whatever
else happens to be installed.
"""
