"""Windows Shell file operations used by the tree context menu."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from ...services.retrieval import assess_path

FO_MOVE = 0x0001
FO_COPY = 0x0002
FO_DELETE = 0x0003
FO_RENAME = 0x0004
FOF_ALLOWUNDO = 0x0040
FOF_WANTNUKEWARNING = 0x4000
COINIT_APARTMENTTHREADED = 0x2


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _multistring(paths: list[str]) -> ctypes.Array:
    if not paths:
        raise ValueError("Choose at least one file or folder.")
    if any(not os.path.isabs(path) or "\0" in path for path in paths):
        raise ValueError("File operations require absolute paths.")
    return ctypes.create_unicode_buffer("\0".join(paths) + "\0\0")


def _validate_live_path(path: str) -> None:
    current = os.path.abspath(path)
    while True:
        parent = os.path.dirname(current)
        flags = getattr(os.lstat(current), "st_file_attributes", 0)
        if flags & 0x400 or (flags & 0x4 and parent != current):
            raise ValueError(f"Cannot modify {path}: {current} is a link/reparse point or system-marked item.")
        if parent == current:
            return
        current = parent


def validate_mutation(paths: list[str]) -> list[dict[str, str]]:
    """Shared UI/execution policy; inspect live attributes before Shell changes."""
    assessments = []
    for path in paths:
        if not os.path.isabs(path) or "\0" in path:
            raise ValueError("File operations require absolute paths without NUL characters.")
        assessment = assess_path(path)
        if assessment["risk"] in {"protected", "managed_cleanup"}:
            raise ValueError(f"Cannot directly modify {path}. {assessment['guidance']}")
        _validate_live_path(path)
        assessments.append(assessment)
    return assessments


def shell_file_operation(
    action: str,
    sources: list[str],
    destination: str | None = None,
    owner_hwnd: int = 0,
) -> bool:
    """Run a Shell copy/move/rename/recycle operation; return False if cancelled."""
    if os.name != "nt":
        raise RuntimeError("Windows Shell file operations require Windows.")
    actions = {"copy": FO_COPY, "move": FO_MOVE, "rename": FO_RENAME, "recycle": FO_DELETE}
    if action not in actions:
        raise ValueError(f"Unsupported file operation: {action}")
    if action in {"copy", "move", "rename"} and not destination:
        raise ValueError("A destination is required for this operation.")
    if action in {"move", "rename", "recycle"}:
        validate_mutation(sources)
        if destination:
            if not os.path.isabs(destination) or "\0" in destination:
                raise ValueError("Destination must be an absolute path without NUL characters.")
            for source in sources:
                target = os.path.join(destination, os.path.basename(source)) if os.path.isdir(destination) else destination
                if assess_path(target)["risk"] in {"protected", "managed_cleanup"}:
                    raise ValueError("Choose a destination outside protected system/application locations.")
                current = os.path.abspath(target)
                # ponytail: preflight is not an atomic transaction with Shell.
                # A future handle-based executor can close replacement races.
                while not os.path.lexists(current):
                    parent = os.path.dirname(current)
                    if parent == current:
                        raise ValueError("Destination drive is unavailable.")
                    current = parent
                _validate_live_path(current)

    from_buffer = _multistring(sources)
    to_buffer = _multistring([destination]) if destination else None
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = owner_hwnd
    operation.wFunc = actions[action]
    operation.pFrom = ctypes.cast(from_buffer, wintypes.LPCWSTR)
    operation.pTo = ctypes.cast(to_buffer, wintypes.LPCWSTR) if to_buffer else None
    operation.fFlags = FOF_ALLOWUNDO | (FOF_WANTNUKEWARNING if action == "recycle" else 0)
    operation.lpszProgressTitle = "SAGE file operation"

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    initialized = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED) >= 0
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
        shell32.SHFileOperationW.restype = ctypes.c_int
        code = shell32.SHFileOperationW(ctypes.byref(operation))
        if code:
            raise OSError(code, f"Windows Shell operation failed (code {code}).")
        return not bool(operation.fAnyOperationsAborted)
    finally:
        if initialized:
            ole32.CoUninitialize()
