"""Enumerate NTFS records by sequentially reading $MFT, like WizTree.

Sizes come from each record's unnamed $DATA, so a second directory-walk size
pass is unnecessary. There is no os.walk() fallback: a failed native scan stays
visible.
"""

from __future__ import annotations

import ctypes
import os
import struct
from collections.abc import Callable, Iterator
from ctypes import wintypes

from ...config import FILE_ATTRIBUTE_DIRECTORY, MFT_BUFFER_SIZE, MFT_PROGRESS_INTERVAL
from ...models import FileRecord

FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_BEGIN = 0
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
USN_RECORD_V2_HEADER_SIZE = 60
NTFS_RECORD_NUMBER_MASK = (1 << 48) - 1
MFT_USA_STRIDE = 512
ATTR_STANDARD_INFORMATION = 0x10
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_REPARSE_POINT = 0xC0
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# Junctions and symlinks set this tag bit; OneDrive, WOF-compressed and dedup files do not.
REPARSE_TAG_NAME_SURROGATE = 0x20000000
FILETIME_UNIX_EPOCH = 116444736000000000
ATTR_END = 0xFFFFFFFF
MFT_IN_USE = 0x0001
MFT_IS_DIRECTORY = 0x0002
_NAME_NS_RANK = {3: 3, 1: 2, 0: 1, 2: 0}


class NTFS_VOLUME_DATA_BUFFER(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_longlong),
        ("NumberSectors", ctypes.c_longlong),
        ("TotalClusters", ctypes.c_longlong),
        ("FreeClusters", ctypes.c_longlong),
        ("TotalReserved", ctypes.c_longlong),
        ("BytesPerSector", wintypes.DWORD),
        ("BytesPerCluster", wintypes.DWORD),
        ("BytesPerFileRecordSegment", wintypes.DWORD),
        ("ClustersPerFileRecordSegment", wintypes.DWORD),
        ("MftValidDataLength", ctypes.c_longlong),
        ("MftStartLcn", ctypes.c_longlong),
        ("Mft2StartLcn", ctypes.c_longlong),
        ("MftZoneStart", ctypes.c_longlong),
        ("MftZoneEnd", ctypes.c_longlong),
    ]


def apply_mft_fixup(record: bytearray, offset: int = 0, record_size: int | None = None) -> bool:
    """Restore the Update Sequence Array so sector-end bytes are real attribute data."""
    if record_size is None:
        record_size = len(record) - offset
    end = offset + record_size
    if end > len(record) or record_size < 8:
        return False
    usa_offset, usa_count = struct.unpack_from("<HH", record, offset + 4)
    usa_abs = offset + usa_offset
    if usa_count < 2 or usa_abs + usa_count * 2 > end:
        return False
    usn = record[usa_abs : usa_abs + 2]
    for i in range(1, usa_count):
        sector_end = offset + i * MFT_USA_STRIDE
        if sector_end > end or sector_end < offset + 2:
            return False
        if record[sector_end - 2 : sector_end] != usn:
            return False
        src = usa_abs + i * 2
        record[sector_end - 2 : sector_end] = record[src : src + 2]
    return True


def parse_data_runs(data: bytes) -> list[tuple[int, int]]:
    """Decode NTFS mapping pairs into (lcn, cluster_count) tuples; lcn is -1 if sparse."""
    runs: list[tuple[int, int]] = []
    offset = 0
    lcn = 0
    length = len(data)
    while offset < length:
        header = data[offset]
        offset += 1
        if header == 0:
            break
        length_size = header & 0x0F
        offset_size = header >> 4
        if length_size == 0 or offset + length_size + offset_size > length:
            break
        cluster_count = int.from_bytes(data[offset : offset + length_size], "little")
        offset += length_size
        if cluster_count <= 0:
            break
        if offset_size == 0:
            runs.append((-1, cluster_count))
            continue
        raw = data[offset : offset + offset_size]
        rel = int.from_bytes(raw, "little")
        bits = offset_size * 8
        if rel >= 1 << (bits - 1):
            rel -= 1 << bits
        lcn += rel
        if lcn < 0:
            break
        runs.append((lcn, cluster_count))
        offset += offset_size
    return runs


def parse_mft_file_record(
    record: bytes | bytearray | memoryview,
) -> tuple[int, int, bool, str, int, int, int, int, int] | None:
    """Return (base_ref, sequence, is_directory, name, parent_frn, logical, allocated, modified, attributes).

    modified is Unix seconds from $STANDARD_INFORMATION; attributes are its Windows file attributes.
    """
    if len(record) < 0x30 or record[0:4] != b"FILE":
        return None
    flags = struct.unpack_from("<H", record, 0x16)[0]
    if not (flags & MFT_IN_USE):
        return None
    sequence = struct.unpack_from("<H", record, 0x10)[0]
    first_attr = struct.unpack_from("<H", record, 0x14)[0]
    used = struct.unpack_from("<I", record, 0x18)[0]
    base_ref = struct.unpack_from("<Q", record, 0x20)[0]
    is_directory = bool(flags & MFT_IS_DIRECTORY)
    limit = min(used, len(record))
    name = ""
    parent_frn = 0
    name_rank = -1
    logical = 0
    allocated = 0
    modified = 0
    attributes = 0
    reparse_tag = None
    offset = first_attr
    while offset + 16 <= limit:
        attr_type = struct.unpack_from("<I", record, offset)[0]
        if attr_type == ATTR_END:
            break
        attr_len = struct.unpack_from("<I", record, offset + 4)[0]
        if attr_len < 16 or offset + attr_len > limit:
            break
        non_resident = record[offset + 8]
        name_len = record[offset + 9]
        if attr_type in (ATTR_STANDARD_INFORMATION, ATTR_REPARSE_POINT) and not non_resident:
            value_len = struct.unpack_from("<I", record, offset + 0x10)[0]
            value_start = offset + struct.unpack_from("<H", record, offset + 0x14)[0]
            if attr_type == ATTR_STANDARD_INFORMATION and value_len >= 0x24 and value_start + 0x24 <= limit:
                filetime = struct.unpack_from("<Q", record, value_start + 8)[0]
                modified = max(0, (filetime - FILETIME_UNIX_EPOCH) // 10_000_000)
                attributes = struct.unpack_from("<I", record, value_start + 0x20)[0]
            elif attr_type == ATTR_REPARSE_POINT and value_len >= 4 and value_start + 4 <= limit:
                reparse_tag = struct.unpack_from("<I", record, value_start)[0]
        elif attr_type == ATTR_FILE_NAME and not non_resident:
            parsed = _file_name_attr(record, offset)
            if parsed is not None:
                candidate, parent, rank = parsed
                if rank > name_rank:
                    name, parent_frn, name_rank = candidate, parent, rank
        elif attr_type == ATTR_DATA and name_len == 0:
            sizes = _unnamed_data_sizes(record, offset, non_resident)
            if sizes is not None:
                logical, allocated = sizes
        offset += attr_len
    if reparse_tag is not None and not reparse_tag & REPARSE_TAG_NAME_SURROGATE:
        attributes &= ~FILE_ATTRIBUTE_REPARSE_POINT
    return (base_ref, sequence, is_directory, name, parent_frn, logical, allocated, modified, attributes)


def _file_name_attr(record: bytes | bytearray | memoryview, offset: int) -> tuple[str, int, int] | None:
    value_len = struct.unpack_from("<I", record, offset + 0x10)[0]
    value_off = struct.unpack_from("<H", record, offset + 0x14)[0]
    value_start = offset + value_off
    if value_off < 0x18 or value_len < 0x42 or value_start + value_len > len(record):
        return None
    parent = struct.unpack_from("<Q", record, value_start)[0]
    chars = record[value_start + 0x40]
    namespace = record[value_start + 0x41]
    name_end = value_start + 0x42 + chars * 2
    if name_end > value_start + value_len:
        return None
    name = bytes(record[value_start + 0x42 : name_end]).decode("utf-16-le", errors="replace")
    if not name:
        return None
    return name, parent, _NAME_NS_RANK.get(namespace, 0)


def _unnamed_data_sizes(
    record: bytes | bytearray | memoryview, offset: int, non_resident: int
) -> tuple[int, int] | None:
    if non_resident:
        if offset + 0x40 > len(record):
            return None
        lowest_vcn = struct.unpack_from("<q", record, offset + 0x10)[0]
        if lowest_vcn != 0:
            return None
        allocated = max(0, struct.unpack_from("<q", record, offset + 0x28)[0])
        logical = max(0, struct.unpack_from("<q", record, offset + 0x30)[0])
        return logical, allocated
    value_len = struct.unpack_from("<I", record, offset + 0x10)[0]
    return max(0, value_len), 0


def parse_usn_records(data: bytes) -> Iterator[FileRecord]:
    """Parse the records after the leading next-FRN cursor."""
    offset = 8
    length = len(data)
    while offset + USN_RECORD_V2_HEADER_SIZE <= length:
        record_length = struct.unpack_from("<I", data, offset)[0]
        if record_length < USN_RECORD_V2_HEADER_SIZE or offset + record_length > length:
            break

        major_version = struct.unpack_from("<H", data, offset + 4)[0]
        if major_version == 2:
            frn = struct.unpack_from("<Q", data, offset + 8)[0]
            parent_frn = struct.unpack_from("<Q", data, offset + 16)[0]
            attributes = struct.unpack_from("<I", data, offset + 52)[0]
            name_length = struct.unpack_from("<H", data, offset + 56)[0]
            name_offset = struct.unpack_from("<H", data, offset + 58)[0]
            name_start = offset + name_offset
            name_end = name_start + name_length
            if name_end <= offset + record_length:
                name = data[name_start:name_end].decode("utf-16-le", errors="replace")
                yield FileRecord(frn, parent_frn, name, attributes)

        offset += record_length


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int64,
        ctypes.POINTER(ctypes.c_int64),
        wintypes.DWORD,
    ]
    kernel32.SetFilePointerEx.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _volume_data(kernel32, handle: int) -> NTFS_VOLUME_DATA_BUFFER:
    volume = NTFS_VOLUME_DATA_BUFFER()
    returned = wintypes.DWORD()
    ok = kernel32.DeviceIoControl(
        handle,
        FSCTL_GET_NTFS_VOLUME_DATA,
        None,
        0,
        ctypes.byref(volume),
        ctypes.sizeof(volume),
        ctypes.byref(returned),
        None,
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    if (
        volume.BytesPerSector == 0
        or volume.BytesPerCluster == 0
        or volume.BytesPerFileRecordSegment == 0
        or volume.MftValidDataLength <= 0
    ):
        raise OSError("NTFS volume geometry is unreadable.")
    return volume


def _read_at(kernel32, handle: int, offset: int, size: int, sector: int) -> bytes:
    if size <= 0:
        return b""
    aligned_off = offset - (offset % sector)
    aligned_end = ((offset + size + sector - 1) // sector) * sector
    aligned_size = aligned_end - aligned_off
    buf = ctypes.create_string_buffer(aligned_size)
    if not kernel32.SetFilePointerEx(handle, aligned_off, None, FILE_BEGIN):
        raise ctypes.WinError(ctypes.get_last_error())
    read = wintypes.DWORD()
    if not kernel32.ReadFile(handle, buf, aligned_size, ctypes.byref(read), None):
        raise ctypes.WinError(ctypes.get_last_error())
    start = offset - aligned_off
    return buf.raw[start : start + min(size, max(0, read.value - start))]


def _mft_data_runs(record: bytearray) -> list[tuple[int, int]]:
    # ponytail: mapping pairs come from $MFT record 0 only. If that record uses
    # $ATTRIBUTE_LIST, scan fails; follow the list if a volume ever hits that.
    if not apply_mft_fixup(record):
        raise OSError("$MFT record 0 failed Update Sequence fixup.")
    parsed_runs: list[tuple[int, int]] = []
    offset = struct.unpack_from("<H", record, 0x14)[0]
    used = min(struct.unpack_from("<I", record, 0x18)[0], len(record))
    while offset + 16 <= used:
        attr_type = struct.unpack_from("<I", record, offset)[0]
        if attr_type == ATTR_END:
            break
        attr_len = struct.unpack_from("<I", record, offset + 4)[0]
        if attr_len < 16 or offset + attr_len > used:
            break
        if attr_type == ATTR_DATA and record[offset + 9] == 0:
            if record[offset + 8] == 0:
                raise OSError("$MFT unnamed $DATA is resident.")
            pairs_off = struct.unpack_from("<H", record, offset + 0x20)[0]
            parsed_runs = parse_data_runs(bytes(record[offset + pairs_off : offset + attr_len]))
            break
        offset += attr_len
    if not parsed_runs:
        raise OSError("Could not read $MFT data runs from record 0.")
    return parsed_runs


def enumerate_mft(
    drive: str,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[dict[int, FileRecord], int]:
    """Return active NTFS records keyed by file reference number, with sizes filled."""
    if os.name != "nt":
        raise RuntimeError("Native MFT enumeration requires Windows.")

    kernel32 = _kernel32()
    volume_path = "\\\\.\\" + drive.rstrip("\\/")
    handle = kernel32.CreateFileW(
        volume_path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    records: dict[int, FileRecord] = {}
    skipped = 0
    last_reported = 0
    extension_sizes: dict[int, tuple[int, int]] = {}
    progress_details = getattr(progress, "on_details", None)

    try:
        geometry = _volume_data(kernel32, handle)
        sector = geometry.BytesPerSector
        cluster = geometry.BytesPerCluster
        record_size = geometry.BytesPerFileRecordSegment
        valid = geometry.MftValidDataLength
        first = _read_at(
            kernel32,
            handle,
            geometry.MftStartLcn * cluster,
            record_size,
            sector,
        )
        if len(first) < record_size:
            raise OSError("Could not read $MFT record 0.")
        runs = _mft_data_runs(bytearray(first))
        if progress:
            progress(0)
        if progress_details:
            progress_details(0, 0.0)

        pending = bytearray()
        # A few hundred distinct attribute values cover millions of records; share one int each.
        attribute_values: dict[int, int] = {}
        record_index = 0
        bytes_seen = 0

        def consume_pending() -> None:
            nonlocal last_reported, record_index, skipped
            usable = len(pending) - (len(pending) % record_size)
            rec_off = 0
            while rec_off < usable:
                if pending[rec_off : rec_off + 4] == b"FILE":
                    if apply_mft_fixup(pending, rec_off, record_size):
                        parsed = parse_mft_file_record(
                            memoryview(pending)[rec_off : rec_off + record_size]
                        )
                        if parsed is not None:
                            base_ref, sequence, is_directory, name, parent_frn, logical, allocated, modified, file_attributes = parsed
                            frn = (record_index & NTFS_RECORD_NUMBER_MASK) | (sequence << 48)
                            if base_ref:
                                if logical or allocated:
                                    extension_sizes[base_ref & NTFS_RECORD_NUMBER_MASK] = (
                                        logical,
                                        allocated,
                                    )
                                skipped += 1
                            elif name:
                                file_attributes |= FILE_ATTRIBUTE_DIRECTORY if is_directory else 0
                                records[frn] = FileRecord(
                                    frn,
                                    parent_frn,
                                    name,
                                    attribute_values.setdefault(file_attributes, file_attributes),
                                    logical_size=logical,
                                    allocated_size=allocated,
                                    size_ready=True,
                                    modified=modified,
                                )
                            else:
                                skipped += 1
                        else:
                            skipped += 1
                record_index += 1
                rec_off += record_size
            del pending[:usable]
            if (progress or progress_details) and len(records) - last_reported >= MFT_PROGRESS_INTERVAL:
                fraction = min(0.99, bytes_seen / valid) if valid else None
                if progress:
                    progress(len(records))
                if progress_details:
                    progress_details(len(records), fraction)
                last_reported = len(records)

        for lcn, cluster_count in runs:
            if cancelled and cancelled():
                raise InterruptedError("MFT scan cancelled.")
            run_bytes = cluster_count * cluster
            take = min(run_bytes, max(0, valid - bytes_seen))
            if take <= 0:
                break
            if lcn < 0:
                consume_pending()
                pending.clear()
                record_index += take // record_size
                bytes_seen += take
                continue
            disk_offset = lcn * cluster
            remaining = take
            while remaining > 0:
                if cancelled and cancelled():
                    raise InterruptedError("MFT scan cancelled.")
                chunk = min(remaining, MFT_BUFFER_SIZE)
                data = _read_at(kernel32, handle, disk_offset, chunk, sector)
                if not data:
                    break
                pending.extend(data)
                disk_offset += len(data)
                remaining -= len(data)
                bytes_seen += len(data)
                consume_pending()
        consume_pending()
    finally:
        kernel32.CloseHandle(handle)

    for frn, record in records.items():
        extra = extension_sizes.get(frn & NTFS_RECORD_NUMBER_MASK)
        if extra and not record.is_directory and record.logical_size == 0 and record.allocated_size == 0:
            record.logical_size, record.allocated_size = extra

    if progress:
        progress(len(records))
    if progress_details:
        progress_details(len(records), 1.0)
    return records, skipped
