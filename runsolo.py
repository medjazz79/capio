"""Run each named test completely alone, to separate real bugs from order
dependence. A test that passes alone but fails in its class is an isolation
problem; a test that fails both ways is a real defect.

    runsolo.py tests.test_app.TestChain.test_foo   # one id
    runsolo.py tests.test_app.TestReaderZoom       # every test in a class
    runsolo.py tests.test_app.TestReaderZoom.test_bar tests.test_app.TestX.y

Test ids are read from the command line, not from a list baked into this file:
a hardcoded list goes stale the moment a test is renamed, and then the runner
silently reports on tests that no longer exist. Ids are printed in full at the
top of the output so a copy-paste from `runtests.py` is always the source of
truth.

Ids may also be piped in, one per line, which is how you feed it a failure list:

    runtests.py 2>&1 | runsolo.py -
"""
import os
import pathlib
import subprocess
import sys

CHILD = r"""
import os, sys, unittest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
rep = open(os.environ["REPORT"], "w", encoding="utf-8")
suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[1])
res = unittest.TextTestRunner(verbosity=0, stream=rep).run(suite)
rep.write("RUN %d FAILED %d\n" % (res.testsRun, len(res.failures) + len(res.errors)))
for t, tr in res.failures + res.errors:
    rep.write("WHY " + t.id() + "\n")
    rep.write("\n".join(tr.strip().splitlines()[-6:]) + "\n---\n")
rep.close()
import os as _o; _o._exit(0)
"""

def resolve(argv: list[str]) -> list[str]:
    """Test ids to run: from argv, or from stdin when the argument is `-`.

    A bare class name (`TestReaderZoom`) is expanded to every test it contains,
    because "does this class pass alone?" is the question this script exists to
    answer and spelling out 90 ids by hand defeats it.
    """
    if argv == ["-"] or not argv:
        argv = [ln.strip() for ln in sys.stdin if ln.strip()]
    if not argv:
        return []

    import unittest
    sys.path.insert(0, os.getcwd())
    out: list[str] = []
    for name in argv:
        try:
            suite = unittest.defaultTestLoader.loadTestsFromName(name)
        except Exception:
            out.append(name)
            continue
        if isinstance(suite, unittest.TestSuite) and suite.countTestCases() > 1:
            out.extend(t.id() for t in suite)
        elif suite.countTestCases() == 1:
            out.extend(t.id() for t in suite)
        else:
            out.append(name)
    return out


TARGETS = resolve(sys.argv[1:])
if not TARGETS:
    print(__doc__)
    sys.stdout.flush()
    os._exit(2)

report = os.path.abspath("solo_report.txt")
os.environ["REPORT"] = report
lines = [f"running {len(TARGETS)} test(s) completely alone", ""]
print(f"running {len(TARGETS)} test(s) completely alone")
real = order = 0
for name in TARGETS:
    if os.path.exists(report):
        os.remove(report)
    completed = subprocess.run([sys.executable, "-c", CHILD, name], check=False,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    text = ""
    if os.path.exists(report):
        text = pathlib.Path(report).read_text(encoding="utf-8")
    # No report at all means the child never got to write one: a bad id, or a
    # native crash on teardown. Counting that as a pass would be a lie.
    if "RUN " not in text:
        code = completed.returncode
        shown = code & 0xFFFFFFFF
        lines.append(f"CRASH {'no report written':<34} {name.split('.')[-1]}"
                     f"  (child exit {shown} / 0x{shown:08X})")
        print(f"CRASH  {name}  (child exit {shown} / 0x{shown:08X})")
        real += 1
        continue
    failed = " FAILED " in text
    verdict = "REAL BUG" if failed else "order-dependent (passes alone)"
    if failed:
        real += 1
    else:
        order += 1
    short = name.split(".")[-1]
    lines.append(f"{'FAIL' if failed else 'pass'}  {verdict:<34} {short}")
    print(f"{'FAIL' if failed else 'pass'}  {short}")
    if failed:
        detail = text.split("---")[0].replace("WHY " + name, "")[:700]
        lines.append(detail)
        lines.append("")

pathlib.Path("solo.txt").write_text("\n".join(lines), encoding="utf-8")
print(f"real bugs: {real}   order-dependent: {order}")
sys.stdout.flush()
os._exit(0)
