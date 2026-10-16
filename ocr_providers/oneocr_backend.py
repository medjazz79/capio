"""Windows 11 native OCR (the Snipping Tool engine), used strictly in place.

Microsoft's files (oneocr.dll, oneocr.onemodel, onnxruntime.dll) are NEVER
bundled, copied into the repo, or distributed with this app. They remain
Microsoft property and are used from the user's own Windows 11 installation:
loaded directly when the OS allows it, otherwise staged once into the user's
own LocalAppData (same machine, same Windows license) — because WindowsApps
blocks in-place DLL loading (Access Denied) for external processes, a fact
verified on this machine (read OK, LoadLibrary -> WinError 5).

Interface knowledge (function names, structs, model key) comes from the open
source reverse-engineering work of b1tg/win11-oneocr, AuroraWright/oneocr and
wangfu91/oneocr-rs. The binding below is written fresh; no third-party code is
vendored.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import threading
import time
from ctypes import POINTER, byref, c_char_p, c_float, c_int32, c_int64, c_ubyte
from pathlib import Path

from .base import OcrBox, OcrLine, OcrResult, OcrUnavailable, OCRProvider, OcrWord

log = logging.getLogger("harmony_translator.ocr.oneocr")

NEEDED_FILES = ("oneocr.dll", "oneocr.onemodel", "onnxruntime.dll")
# Published model-unlock key used by every open source OneOCR consumer.
MODEL_KEY = b"kj)TGtrK>f]b[Piow.gU+nC@s\"\"\"\"\"\"4"
MIN_SIDE_PX = 60
MAX_SIDE_PX = 10000

_c_int64_p = POINTER(c_int64)
_c_float_p = POINTER(c_float)
_c_ubyte_p = POINTER(c_ubyte)


class _Image(ctypes.Structure):
    _fields_ = [
        ("type", c_int32),
        ("width", c_int32),
        ("height", c_int32),
        ("_reserved", c_int32),
        ("step", c_int64),
        ("data", _c_ubyte_p),
    ]


class _BBox(ctypes.Structure):
    _fields_ = [(n, c_float) for n in
                ("x1", "y1", "x2", "y2", "x3", "y3", "x4", "y4")]


_BBox_p = POINTER(_BBox)

_FUNCTIONS = [
    ("CreateOcrInitOptions", [_c_int64_p], c_int64),
    ("OcrInitOptionsSetUseModelDelayLoad", [c_int64, ctypes.c_char], c_int64),
    ("CreateOcrPipeline", [c_char_p, c_char_p, c_int64, _c_int64_p], c_int64),
    ("CreateOcrProcessOptions", [_c_int64_p], c_int64),
    ("OcrProcessOptionsSetMaxRecognitionLineCount", [c_int64, c_int64], c_int64),
    ("RunOcrPipeline", [c_int64, POINTER(_Image), c_int64, _c_int64_p], c_int64),
    ("GetImageAngle", [c_int64, _c_float_p], c_int64),
    ("GetOcrLineCount", [c_int64, _c_int64_p], c_int64),
    ("GetOcrLine", [c_int64, c_int64, _c_int64_p], c_int64),
    ("GetOcrLineContent", [c_int64, POINTER(c_char_p)], c_int64),
    ("GetOcrLineBoundingBox", [c_int64, POINTER(_BBox_p)], c_int64),
    ("GetOcrLineWordCount", [c_int64, _c_int64_p], c_int64),
    ("GetOcrWord", [c_int64, c_int64, _c_int64_p], c_int64),
    ("GetOcrWordContent", [c_int64, POINTER(c_char_p)], c_int64),
    ("GetOcrWordBoundingBox", [c_int64, POINTER(_BBox_p)], c_int64),
    ("GetOcrWordConfidence", [c_int64, _c_float_p], c_int64),
    ("ReleaseOcrResult", [c_int64], None),
    ("ReleaseOcrInitOptions", [c_int64], None),
    ("ReleaseOcrPipeline", [c_int64], None),
    ("ReleaseOcrProcessOptions", [c_int64], None),
]


_dir_cache: dict = {}
_dir_cache_lock = threading.Lock()
_NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# How long a "not found" answer is believed. A hit is cached for the process
# lifetime; a miss is only cached this long, because the probe spawns
# PowerShell and can fail for reasons that pass (a locked-down shell, a
# timeout, an AppX query hiccup). Caching a miss forever meant one transient
# failure disabled OCR until restart, and the user was told to install the
# Snipping Tool they already had.
_DIR_MISS_TTL_SECONDS = 60.0


def find_snipping_dir() -> Path | None:
    """Locate the Snipping Tool install dir dynamically (version-independent)."""
    now = time.monotonic()
    with _dir_cache_lock:
        cached = _dir_cache.get("dir")
        if cached is not None:
            found, stamp = cached
            if found is not None or now - stamp < _DIR_MISS_TTL_SECONDS:
                return found
    found = _find_snipping_dir_uncached()
    with _dir_cache_lock:
        _dir_cache["dir"] = (found, time.monotonic())
    return found


def _find_snipping_dir_uncached() -> Path | None:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AppxPackage -Name 'Microsoft.ScreenSketch' | "
             "Select-Object -First 1).InstallLocation"],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_CONSOLE_WINDOW)
    except Exception:
        # Swallowed silently before, so a permissions error and a genuine
        # "not installed" were indistinguishable and both read as "install the
        # Snipping Tool". Log the real reason.
        log.warning(
            "Could not query the Snipping Tool install location",
            exc_info=True,
        )
        return None
    base = (out.stdout or "").strip()
    if not base:
        log.warning(
            "PowerShell returned no ScreenSketch install location "
            "(exit code %s): %s", out.returncode, (out.stderr or "").strip(),
        )
        return None
    candidate = Path(base) / "SnippingTool"
    missing = [f for f in NEEDED_FILES if not (candidate / f).is_file()]
    if missing:
        log.warning(
            "Snipping Tool directory %s is missing %s", candidate,
            ", ".join(missing),
        )
        return None
    return candidate


def stage_dir() -> Path:
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / "HarmonyTranslator" / "oneocr"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _same_file(a: Path, b: Path) -> bool:
    try:
        sa, sb = a.stat(), b.stat()
        return sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime)
    except OSError:
        return False


def ensure_usable_dir() -> Path:
    """Return a directory from which oneocr.dll can actually be loaded.

    Prefers the Snipping Tool folder itself; WindowsApps blocks external
    DLL loading, so the verified fallback stages one copy per user into
    LocalAppData (re-copied only when the source changes).
    """
    src = find_snipping_dir()
    if src is None:
        raise OcrUnavailable("Snipping Tool (Microsoft.ScreenSketch) not found")
    try:
        dll = ctypes.WinDLL(str(src / "oneocr.dll"))
        del dll
        return src
    except OSError:
        pass  # expected on stock Windows: Access Denied -> stage locally
    dst = stage_dir()
    for f in NEEDED_FILES:
        s, d = src / f, dst / f
        if not d.is_file() or not _same_file(s, d):
            shutil.copy2(s, d)
    try:
        dll = ctypes.WinDLL(str(dst / "oneocr.dll"))
        del dll
        return dst
    except OSError as e:
        raise OcrUnavailable(f"OneOCR cannot be loaded: {e}") from e


def _status_text(code: int) -> str:
    """Render a native status code so the real cause is legible.

    OneOCR returns HRESULTs; `str(0x80004005)` prints as 2147500037, which is
    useless in a message a user might paste into a bug report.
    """
    if isinstance(code, int) and not (-0x8000_0000 <= code <= 0x7FFF_FFFF):
        return f"0x{code & 0xFFFF_FFFF:08X}"
    return str(code)


class _NativeEngine:
    """Raw ctypes binding. One instance per process; external lock serializes use."""

    def __init__(self, directory: Path):
        self._directory = directory
        self._dll = None
        self._opts = None
        self._pipe = None
        self._popts = None
        self._dll_dir_handle = None
        self._previous_dll_dir = None
        self._owns_dll_directory = False
        self._install_dll_directory(directory)
        try:
            dll = ctypes.WinDLL(str(directory / "oneocr.dll"))
        except OSError as e:
            self.close()
            raise OcrUnavailable(f"Cannot load oneocr.dll: {e}") from e
        for name, argtypes, restype in _FUNCTIONS:
            try:
                func = getattr(dll, name)
            except AttributeError as e:
                self.close()
                raise OcrUnavailable(
                    f"oneocr.dll misses export {name}") from e
            func.argtypes = argtypes
            func.restype = restype
        self._dll = dll

        # Each handle is recorded the moment it is acquired, so a failure at
        # any later stage can hand back everything already taken. Without this,
        # `self._ok` raised with the earlier handles still live, and because
        # `_engine_locked` only caches on success every retry leaked them again.
        try:
            opts = c_int64()
            self._ok(dll.CreateOcrInitOptions(byref(opts)), "init options")
            self._opts = opts.value
            self._ok(dll.OcrInitOptionsSetUseModelDelayLoad(opts, 0), "delay load")

            model = ctypes.create_string_buffer(
                str(directory / "oneocr.onemodel").encode())
            key = ctypes.create_string_buffer(MODEL_KEY)
            pipe = c_int64()
            self._ok(
                dll.CreateOcrPipeline(model, key, opts, byref(pipe)), "pipeline")
            self._pipe = pipe.value

            popts = c_int64()
            self._ok(dll.CreateOcrProcessOptions(byref(popts)), "process options")
            self._popts = popts.value
            self._ok(dll.OcrProcessOptionsSetMaxRecognitionLineCount(popts, 1000),
                     "line count")
        except Exception:
            self.close()
            raise

    def _install_dll_directory(self, directory: Path) -> None:
        """Point the loader at `directory` and remember how to undo it.

        Both mechanisms used here are process-wide. SetDllDirectoryW *replaces*
        the search directory and os.add_dll_directory appends to it; neither was
        undone, and the add_dll_directory handle was discarded outright, so the
        mutation outlived the engine for the whole process.
        """
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except Exception:
            log.debug("Could not load kernel32 for SetDllDirectoryW",
                      exc_info=True)
            return
        previous = None
        try:
            if hasattr(kernel32, "SetDllDirectoryW"):
                kernel32.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
                kernel32.SetDllDirectoryW.restype = ctypes.c_int
                get_directory = getattr(kernel32, "GetDllDirectoryW", None)
                if get_directory is not None:
                    get_directory.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p]
                    get_directory.restype = ctypes.c_uint32
                    buffer = ctypes.create_unicode_buffer(1024)
                    if get_directory(len(buffer), buffer):
                        previous = buffer.value
                kernel32.SetDllDirectoryW(str(directory))
                self._previous_dll_dir = previous
                self._owns_dll_directory = True
        except Exception:
            log.warning("Could not set the OneOCR DLL directory", exc_info=True)
        try:
            self._dll_dir_handle = os.add_dll_directory(str(directory))
        except Exception:
            log.debug("os.add_dll_directory failed", exc_info=True)
            self._dll_dir_handle = None

    @staticmethod
    def _ok(code: int, what: str) -> None:
        if code != 0:
            raise OcrUnavailable(f"OneOCR {what} failed (code {code})")

    def recognize(self, bgra: bytes, width: int, height: int) -> OcrResult:
        data = (c_ubyte * len(bgra)).from_buffer_copy(bgra)
        img = _Image(3, width, height, 0, width * 4, data)
        result = c_int64()
        code = self._dll.RunOcrPipeline(
            self._pipe, byref(img), self._popts, byref(result))
        if code != 0:
            # Returning OcrResult(text="") here made popup/worker.py report
            # "Could not read any text from this image. Try selecting a bigger
            # area", which is wrong advice when the engine itself failed: the
            # real cause has to reach the user.
            raise OcrUnavailable(
                f"OneOCR failed to recognise this image "
                f"({width}x{height}, code {_status_text(code)})")
        try:
            return self._parse(result)
        finally:
            self._dll.ReleaseOcrResult(result)

    def _parse(self, result: c_int64) -> OcrResult:
        n = c_int64()
        code = self._dll.GetOcrLineCount(result, byref(n))
        if code != 0:
            # Same reasoning as RunOcrPipeline: an engine error is not an
            # empty page.
            raise OcrUnavailable(
                f"OneOCR could not read the recognition result "
                f"(code {_status_text(code)})")
        lines = [self._line(result, i) for i in range(n.value)]
        lines = [ln for ln in lines if ln.text]
        angle = c_float()
        ang = None
        if self._dll.GetImageAngle(result, byref(angle)) == 0:
            ang = angle.value
        return OcrResult(text="\n".join(ln.text for ln in lines),
                         lines=lines, backend="OneOCR", angle=ang)

    def _line(self, result: c_int64, idx: int) -> OcrLine:
        handle = c_int64()
        if self._dll.GetOcrLine(result, idx, byref(handle)) != 0:
            return OcrLine(text="")
        return OcrLine(text=self._text(handle, self._dll.GetOcrLineContent) or "",
                       box=self._box(handle, self._dll.GetOcrLineBoundingBox),
                       words=self._words(handle))

    def _words(self, line_handle: c_int64) -> list[OcrWord]:
        n = c_int64()
        if self._dll.GetOcrLineWordCount(line_handle, byref(n)) != 0:
            return []
        out = []
        for i in range(n.value):
            handle = c_int64()
            if self._dll.GetOcrWord(line_handle, i, byref(handle)) != 0:
                continue
            conf = c_float()
            c = None
            if self._dll.GetOcrWordConfidence(handle, byref(conf)) == 0:
                c = conf.value
            out.append(OcrWord(
                text=self._text(handle, self._dll.GetOcrWordContent) or "",
                box=self._box(handle, self._dll.GetOcrWordBoundingBox),
                confidence=c))
        return out

    def _text(self, handle: c_int64, fn) -> str | None:
        content = c_char_p()
        if fn(handle, byref(content)) == 0 and content.value:
            return content.value.decode("utf-8", errors="ignore")
        return None

    def _box(self, handle: c_int64, fn) -> OcrBox | None:
        ptr = POINTER(_BBox)()
        if fn(handle, byref(ptr)) == 0 and ptr:
            b = ptr.contents
            return OcrBox(b.x1, b.y1, b.x2, b.y2, b.x3, b.y3, b.x4, b.y4)
        return None

    def close(self) -> None:
        """Release every native handle and undo the process-wide DLL setup.

        Safe to call more than once and safe to call on a half-built engine:
        `_NativeEngine.__init__` uses it as its own rollback.
        """
        dll, self._dll = self._dll, None
        if dll is not None:
            # Release in reverse acquisition order; each handle is cleared
            # first so a second close() cannot release it twice.
            for attribute, release in (
                ("_popts", "ReleaseOcrProcessOptions"),
                ("_pipe", "ReleaseOcrPipeline"),
                ("_opts", "ReleaseOcrInitOptions"),
            ):
                handle = getattr(self, attribute, None)
                setattr(self, attribute, None)
                if handle is None:
                    continue
                try:
                    getattr(dll, release)(handle)
                except Exception:
                    log.debug("Could not %s", release, exc_info=True)
        handle, self._dll_dir_handle = self._dll_dir_handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                log.debug("Could not close the DLL directory handle",
                          exc_info=True)
        if self._owns_dll_directory:
            self._owns_dll_directory = False
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
                kernel32.SetDllDirectoryW.restype = ctypes.c_int
                # Passing None removes the override, which is what
                # SetDllDirectoryW("") did before; passing the saved value
                # restores whatever the process had before us.
                kernel32.SetDllDirectoryW(self._previous_dll_dir or None)
            except Exception:
                log.warning("Could not restore the DLL search path",
                            exc_info=True)
            self._previous_dll_dir = None


class OneOCRProvider(OCRProvider):
    """Windows 11 native engine. Multilingual by nature; ignores langs hints."""

    name = "OneOCR"

    def __init__(self):
        self._engine: _NativeEngine | None = None
        self._lock = threading.Lock()
        self._dir: Path | None = None

    def is_available(self) -> bool:
        if self._engine is not None:
            return True
        return find_snipping_dir() is not None

    @property
    def warmed(self) -> bool:
        return self._engine is not None

    def warm(self) -> None:
        self._engine_locked()

    def _engine_locked(self) -> _NativeEngine:
        with self._lock:
            if self._engine is None:
                if self._dir is None:
                    self._dir = ensure_usable_dir()
                self._engine = _NativeEngine(self._dir)
            return self._engine

    def close(self) -> None:
        """Release the native engine.

        Without this the pipeline, the process options and the init options
        stayed allocated for the life of the process, and the DLL search path
        the engine installed stayed mutated. Idempotent.
        """
        with self._lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.close()
            except Exception:
                log.debug("Could not close the OneOCR engine", exc_info=True)

    def read(self, pil_image, langs: tuple | None = None) -> OcrResult:
        engine = self._engine_locked()
        img = pil_image.convert("RGB")
        w, h = img.size
        scale = 1.0
        if min(w, h) < MIN_SIDE_PX:
            scale = MIN_SIDE_PX / max(1, min(w, h))
            w, h = int(w * scale), int(h * scale)
            img = img.resize((w, h))
        if max(w, h) > MAX_SIDE_PX:
            raise OcrUnavailable(f"Image too large for OneOCR: {w}x{h}")
        rgba = img.convert("RGBA")
        r, g, b, a = rgba.split()
        from PIL import Image
        bgra = Image.merge("RGBA", (b, g, r, a)).tobytes()
        with self._lock:
            result = engine.recognize(bgra, w, h)
        if scale != 1.0:
            for line in result.lines:
                for box in [line.box] + [wd.box for wd in line.words]:
                    if box is None:
                        continue
                    box.x1 /= scale
                    box.x2 /= scale
                    box.x3 /= scale
                    box.x4 /= scale
                    box.y1 /= scale
                    box.y2 /= scale
                    box.y3 /= scale
                    box.y4 /= scale
        return result
