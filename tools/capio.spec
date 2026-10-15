# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the Capio floating translator.

    pyinstaller tools/capio.spec --noconfirm

Notes that matter (each one cost a failed build before):

* `--paths .` is required. The app puts its own folder on sys.path at runtime
  (main.py / main_02.py bootstrap), but PyInstaller's analysis never executes
  that code, so `config`, `popup`, `reader`, `translators`, `ocr_providers`,
  `ui` would otherwise be "missing".
* `pymupdf` ships a C extension plus its own .pyd/.dll set.
* `PIL` resolves its plugins at runtime through a loader, so submodules must be
  collected explicitly.
* `mss` (screen capture for the floating tool) loads a native helper.
* `edge_tts` (read-aloud) needs certifi's CA bundle.
* OneOCR's dll/onemodel are NOT bundled: they stay Microsoft property and are
  located on the user's own machine at runtime (see ocr_providers/oneocr_backend).
"""
import sys

block_cipher = None

hidden = [
    # first-party packages found through the runtime sys.path insert
    "config", "popup", "reader", "translators", "ocr_providers", "ui",
    "main",  # dynamically opened as the document reader by the floating tool
    "config.app_log",
    # imported inside functions / threads, invisible to the entry point
    "translators.ocr", "translators.chain", "translators.probe",
    "ocr_providers.oneocr_backend", "ocr_providers.manager",
    "popup.translate_popup", "popup.speech_worker",
    "reader.pdf_utils", "reader.thumbnails", "reader.document_import",
    # third-party
    "pymupdf", "mammoth", "mss", "mss.windows", "PIL", "PIL.Image", "PIL.PngImagePlugin",
    "PIL.ImageOps", "edge_tts", "certifi", "langdetect",
]

datas = []
binaries = []

for pkg in ("pymupdf", "mammoth", "PIL", "certifi", "edge_tts"):
    try:
        from PyInstaller.utils.hooks import collect_all
        _d, _b, _h = collect_all(pkg)
        datas += _d
        binaries += _b
        hidden += _h
    except Exception:
        pass

try:
    from PyInstaller.utils.hooks import collect_dynamic_libs
    binaries += collect_dynamic_libs("mss")
    binaries += collect_dynamic_libs("pymupdf")
except Exception:
    pass

a = Analysis(
    ["main_02.py"],          # the floating tool; swap to "main.py" for the reader
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CapioFloat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # no console window: this is a tray/button app
    disable_windowed_traceback=False,
)
