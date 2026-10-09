"""Native volume discovery without third-party packages."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from ...models import VolumeInfo

DRIVE_FIXED = 3


def _kernel32():
    if os.name != "nt":
        raise RuntimeError("NTFS volume discovery is only available on Windows.")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def get_volume_info(drive: str) -> VolumeInfo:
    letter = drive.rstrip("\\/").upper()
    root = letter + "\\"
    label = ctypes.create_unicode_buffer(261)
    filesystem = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()

    kernel32 = _kernel32()
    kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    ok = kernel32.GetVolumeInformationW(
        root,
        label,
        len(label),
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        filesystem,
        len(filesystem),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return VolumeInfo(letter, label.value, filesystem.value)


def list_ntfs_volumes() -> list[VolumeInfo]:
    kernel32 = _kernel32()
    kernel32.GetLogicalDrives.argtypes = []
    kernel32.GetLogicalDrives.restype = wintypes.DWORD
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    mask = kernel32.GetLogicalDrives()
    if not mask:
        raise ctypes.WinError(ctypes.get_last_error())

    volumes: list[VolumeInfo] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        drive = f"{chr(65 + index)}:"
        if kernel32.GetDriveTypeW(drive + "\\") != DRIVE_FIXED:
            continue
        try:
            info = get_volume_info(drive)
        except OSError:
            continue
        if info.filesystem.upper() == "NTFS":
            volumes.append(info)
    return volumes


def get_disk_space(drive: str) -> tuple[int, int, int]:
    """Return (total_bytes, used_bytes, free_bytes) for a volume."""
    kernel32 = _kernel32()
    free_available = ctypes.c_ulonglong()
    total_bytes = ctypes.c_ulonglong()
    free_bytes = ctypes.c_ulonglong()
    root = drive.rstrip("\\/").upper() + "\\"
    ok = kernel32.GetDiskFreeSpaceExW(
        root,
        ctypes.byref(free_available),
        ctypes.byref(total_bytes),
        ctypes.byref(free_bytes),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    total = total_bytes.value
    free = free_bytes.value
    return total, total - free, free
