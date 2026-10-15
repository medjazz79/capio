"""Run test classes in isolated processes and print a clean summary.

The GUI suites interfere with each other when run together: the same test
passed alone and failed in a batch, and the counts moved between runs. Running
each class in its own process removes the shared-state problem and also
survives the Qt teardown crash that kills a combined run.

The large reader and memory/zoom Qt classes also accumulate unstable widget
state on Windows, so their individual methods run in separate processes.

`python -m` is the supported invocation for this project's suite; running a
test file directly (`python tests\\test_app.py`) puts `tests\\` on sys.path
instead of the project root, so `import main` / `import translators` resolve to
whatever else is installed instead of this checkout. `tests/__init__.py` exists
for the same reason: the modules are imported as `tests.test_app`, so the
package must be a real package and not an implicit namespace one.

    python tools\\runtests.py                  -> every class, every test module
    python tools\\runtests.py TestChain        -> one class
    python tools\\runtests.py tests.test_chat  -> one module
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CHILD = r"""
import os, sys, unittest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
name = sys.argv[1]
rep = open(os.environ["REPORT"], "w", encoding="utf-8")
suite = unittest.defaultTestLoader.loadTestsFromName(name)
res = unittest.TextTestRunner(verbosity=0, stream=rep).run(suite)
rep.write("RUN %d FAILED %d\n" % (res.testsRun, len(res.failures) + len(res.errors)))
for t, _ in res.failures + res.errors:
    rep.write("FAILNAME " + t.id() + "\n")
rep.close()
import os as _o; _o._exit(0)
"""


def classes_in(module: str) -> list[str]:
    """Fully-qualified Test* classes defined at module level."""
    import ast
    path = pathlib.Path(module.replace(".", "/") + ".py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [f"{module}.{n.name}" for n in tree.body
            if isinstance(n, ast.ClassDef)
            and n.name.startswith("Test")]


def bare_class(name: str) -> str:
    return name.rsplit(".", 1)[-1]


MODULES = ("tests.test_app", "tests.test_chat",
           "tests.test_engine_fixes", "tests.test_file_menu",
           "tests.test_freellmapi", "tests.test_freellm_vision",
           "tests.test_gemini_image_output_budget",
           "tests.test_language_validation", "tests.test_gemini_streaming",
           "tests.test_image_streaming", "tests.test_reader_fixes",
           "tests.test_speech_edge_only",
           "tests.test_speech_segment_handoff",
           "tests.test_translation_fonts")

ISOLATED_TEST_CLASSES = {
    "tests.test_app.TestReaderZoom",
    "tests.test_app.TestMemoryZoom",
}


def _exists(module: str) -> bool:
    return pathlib.Path(module.replace(".", "/") + ".py").exists()


def test_methods_in(class_name: str) -> list[str]:
    """Fully-qualified test methods defined directly on a test class."""
    import ast
    module, class_name = class_name.rsplit(".", 1)
    path = pathlib.Path(module.replace(".", "/") + ".py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [
                f"{module}.{class_name}.{method.name}"
                for method in node.body
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                and method.name.startswith("test_")
            ]
    return []


def _isolate_test_methods(names: list[str]) -> list[str]:
    isolated = []
    for name in names:
        if name in ISOLATED_TEST_CLASSES:
            isolated.extend(test_methods_in(name) or [name])
        else:
            isolated.append(name)
    return isolated


def targets(argv: list[str]) -> list[str]:
    """Fully-qualified targets.

    `loadTestsFromName` needs a dotted path: given a bare `test_freellmapi` it
    reports a silent zero-test pass, and given `TestChain` it reports a
    `_FailedTest`. Every bare name is therefore resolved against the modules
    that actually exist.
    """
    if not argv:
        out = []
        for module in MODULES:
            if not _exists(module):
                continue
            # A class is normally preferred, because one process per class is
            # what keeps the GUI suites from interfering. A module with no
            # Test* class of its own falls back to being run whole.
            out.extend(classes_in(module) or [module])
        return _isolate_test_methods(out)

    out = []
    for a in argv:
        if "." in a:
            out.append(a)
            continue
        match = None
        for module in MODULES:
            if not _exists(module):
                continue
            for qualified in classes_in(module):
                if bare_class(qualified) == a:
                    match = qualified
                    break
            if match:
                break
        if match:
            out.append(match)
        else:
            print(f"unknown target: {a}", file=sys.stderr)
    return _isolate_test_methods(out)


def main() -> int:
    report_fd, report = tempfile.mkstemp(
        prefix="harmony-runtests-", suffix=".txt")
    os.close(report_fd)
    os.environ["REPORT"] = report
    total = failed = 0
    problems: list[str] = []
    for name in targets(sys.argv[1:]):
        if os.path.exists(report):
            os.remove(report)
        # The app's RotatingFileHandler cannot rotate while a previous run still
        # holds the file, so it floods stderr with "Logging error" blocks. They
        # are not test failures; discard them so a real error stays visible.
        completed = subprocess.run([sys.executable, "-c", CHILD, name],
                                   check=False,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        text = ""
        if os.path.exists(report):
            with open(report, encoding="utf-8") as fh:
                text = fh.read()
        run = failed_here = 0
        for line in text.splitlines():
            if line.startswith("RUN "):
                parts = line.split()
                run, failed_here = int(parts[1]), int(parts[3])
            elif line.startswith("FAILNAME "):
                problems.append(line[len("FAILNAME "):])
        if run == 0:
            # Two different things look identical here: the module could not be
            # imported, or the child died before it could write the report. A
            # native Qt crash on teardown is the second one, and it is invisible
            # unless the child's return code is shown. On Windows an access
            # violation comes back as 3221225477 (0xC0000005), well outside the
            # 0-255 range a Python exit would use.
            code = completed.returncode
            crash = code != 0 and (code < 0 or code > 255)
            suffix = ""
            if code:
                shown = code & 0xFFFFFFFF
                suffix = (f"  child exit {shown} (0x{shown:08X})"
                          + ("  <- CRASH, not an import error" if crash else ""))
            print(f"{name.split('.')[-1]:>34}: LOAD ERROR{suffix}")
            failed += 1
            problems.append(f"{name} (load error{', exit ' + str(code) if code else ''})")
            continue
        total += run
        failed += failed_here
        mark = "ok" if not failed_here else f"{failed_here} FAILED"
        print(f"{name.split('.')[-1]:>34}: {run:>4} run, {mark}")
    print("-" * 46)
    print(f"{'TOTAL':>34}: {total:>4} run, {failed} failed")
    if problems:
        print("\nfailing tests:")
        for p in problems:
            print(f"  {p}")
    if os.path.exists(report):
        os.remove(report)
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    # os._exit skips atexit and buffer flushing, so nothing printed above would
    # reach the shell. Flush explicitly or the run looks silent and empty.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
