"""Windows Shell file operations used by the tree context menu."""

from __future__ import annotations

import ctypes
import os
import shutil
import stat
import sys
from ctypes import wintypes

FO_MOVE = 0x0001
FO_COPY = 0x0002
FO_DELETE = 0x0003
FO_RENAME = 0x0004
FOF_ALLOWUNDO = 0x0040
FOF_WANTNUKEWARNING = 0x4000
COINIT_APARTMENTTHREADED = 0x2
SHERB_SILENT = 0x0007
E_UNEXPECTED = -2147418113


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
    if any(not os.path.isabs(path) for path in paths):
        raise ValueError("File operations require absolute paths.")
    return ctypes.create_unicode_buffer("\0".join(paths) + "\0\0")


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


def delete_permanently(path: str, contents_only: bool = False) -> int:
    """Delete without the Recycle Bin; return how many items could not be removed.

    Files in use are skipped. Symlinks and junctions are removed, never followed.
    """
    absolute = os.path.abspath(path)
    if not os.path.isabs(path) or not os.path.splitdrive(absolute)[1].strip("\\/"):
        raise ValueError("Refusing to delete a drive root or relative path.")
    failures: list[str] = []

    def retry_writable(func, failed: str, _error: object) -> None:
        try:
            os.chmod(failed, stat.S_IWRITE)
            func(failed)
        except OSError:
            failures.append(failed)

    handler = {"onexc": retry_writable} if sys.version_info >= (3, 12) else {"onerror": retry_writable}
    targets = [entry.path for entry in os.scandir(absolute)] if contents_only else [absolute]
    for target in targets:
        try:
            info = os.lstat(target)
            is_link = stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
            if is_link:
                try:
                    os.unlink(target)
                except OSError:
                    os.rmdir(target)
            elif stat.S_ISDIR(info.st_mode):
                shutil.rmtree(target, **handler)
            else:
                retry_writable(os.unlink, target, None)
        except FileNotFoundError:
            continue
        except OSError:
            failures.append(target)
    return len(failures)


def empty_recycle_bin(drive: str) -> None:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
    shell32.SHEmptyRecycleBinW.restype = ctypes.c_long
    code = shell32.SHEmptyRecycleBinW(None, drive.rstrip("\\/") + "\\", SHERB_SILENT)
    if code < 0 and code != E_UNEXPECTED:  # E_UNEXPECTED: the bin was already empty.
        raise OSError(code, f"Could not empty the Recycle Bin (HRESULT {code & 0xFFFFFFFF:#x}).")

