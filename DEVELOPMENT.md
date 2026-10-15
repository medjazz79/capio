# Development

Technical notes for contributors and maintainers.

## Project layout

- `main.py` — book reader entry point.
- `main_02.py` — floating translator entry point.
- `reader/`, `popup/`, `translators/`, `ocr_providers/`, and `ui/` — application components.
- `setup/` — bootstrap script and runtime dependency lists.
- `tools/` — test runner and PyInstaller spec.
- `tests/` — automated test suite.
- `config/` — application logging configuration.

## Environment and tests

Capio supports Python 3.10 or newer. On Windows, `run.bat` and `run_float.bat`
create and maintain the application virtual environment. For a development
environment, install the runtime requirements from `setup/requirements.txt`
and run the isolated test suite with:

```powershell
.\.venv\Scripts\python.exe tools\runtests.py
```

Run one test class by passing its name, for example `TestFloat`, or a test
module such as `tests.test_chat`. The runner isolates test classes in separate
processes to avoid Qt GUI state leaking between tests.

## PDF opening and rendering performance

Some scanned or image-heavy books used to take a long time before their page
thumbnails finished rendering. The main PDF document itself was not necessarily
slow to open: measurements on a 382-page, 19.9 MB scanned book showed document
setup in about 122 ms and the first visible page rendered in about 297 ms, while
all thumbnails took about 37.9 seconds. In an earlier benchmark of that book,
thumbnail rendering took about 37.4 seconds.

The main bottleneck was eager rendering of every thumbnail at three times its
display width, at the same time as the reader was rendering visible pages.
Moving PyMuPDF work to a `QThread` did not isolate it from the GUI process, and
PyMuPDF explicitly warns against using it from multiple threads in one process.

The current implementation addresses this by:

- Rendering the first visible PDF pages before starting the thumbnail batch.
- Limiting thumbnail work to four spawned processes (or fewer on machines with
  fewer CPU cores). Each process opens its own `pymupdf.Document`; do not pass
  a document or page object between processes.
- Rendering thumbnails at the current displayed icon width, with RGB output
  and no alpha channel, rather than rendering oversized images for later
  downscaling.
- Rendering only the visible thumbnail rows plus one prefetch row on either
  side. Rows are rendered as the user scrolls; thumbnails outside that small
  working set are released and regenerated if revisited. Hiding the Pages pane
  releases its thumbnail image cache.
- Using a bounded multiprocessing queue and stopping/joining workers when a
  book is closed. `main.py` calls `multiprocessing.freeze_support()` for
  Windows/frozen application startup.
- Keeping the first-page render pump independent of zoom changes: a no-op
  initial fit must still schedule visible-page rendering.

Before visible-row rendering was added, the updated thumbnail worker rendered
all pages of the same 382-page book in about 11.1 seconds in a measured run,
down from about 37.4 seconds. The Pages pane now renders nearby thumbnails
on demand instead of delaying use while the whole book is processed. The
complete-book time still depends on the PDF: very large embedded scans or
complex vector pages can take longer.

When changing PDF performance code, preserve these constraints and benchmark
the stages separately: document open, page geometry/setup, first visible-page
render, and thumbnail completion. PyMuPDF's supported parallelism approach is
processes, not threads; each process must open the document independently.
`Pixmap.samples_mv` avoids an extra Python bytes copy, but a `QImage` wrapping
that memory must be copied before the pixmap goes out of scope.

References: [PyMuPDF multiprocessing recipe](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html),
[PyMuPDF threading FAQ](https://pymupdf.readthedocs.io/en/latest/faq/index.html#does-pymupdf-support-multi-threading),
[Pixmap.samples_mv](https://pymupdf.readthedocs.io/en/latest/pixmap.html#Pixmap.samples_mv).

## Build a single .exe (optional)

Use the checked-in spec file rather than a bare `--onefile` line. The spec is
the only place that declares the `mss` and `pymupdf` shared libraries PyInstaller
otherwise drops, and it excludes the packages that make a one-file build slow.

```bat
pip install pyinstaller
pyinstaller tools\capio.spec
```

Note what that spec actually builds: **`main_02.py`**, named
**`CapioFloat`** — the floating translator, not the main reader. To
build the reader instead, change the `["main_02.py"]` entry in
`tools\capio.spec` to `["main.py"]` and update the `EXE` name accordingly.

## Notes

- Chord symbols, figured-bass numbers and measure numbers are never translated.
