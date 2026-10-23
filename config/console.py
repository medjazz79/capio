"""Windows console helpers for the GUI entry points."""
from __future__ import annotations

import os


def minimize_standalone_console() -> None:
    """Minimize this app's console without touching an existing user shell."""
    if os.name != "nt":
        return

    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_console_window = kernel32.GetConsoleWindow
    get_console_window.restype = ctypes.c_void_p
    window = get_console_window()
    if not window:
        return

    get_console_process_list = kernel32.GetConsoleProcessList
    get_console_process_list.argtypes = [
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_ulong,
    ]
    get_console_process_list.restype = ctypes.c_ulong
    process_ids = (ctypes.c_ulong * 16)()
    if get_console_process_list(process_ids, len(process_ids)) != 1:
        return

    show_window = user32.ShowWindow
    show_window.argtypes = [ctypes.c_void_p, ctypes.c_int]
    show_window.restype = ctypes.c_bool
    show_window(window, 7)  # SW_SHOWMINNOACTIVE: taskbar, without stealing focus.
