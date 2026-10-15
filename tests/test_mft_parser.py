import struct
import unittest

from sage.config import FILE_ATTRIBUTE_DIRECTORY
from sage.platform.windows.mft import (
    apply_mft_fixup,
    parse_data_runs,
    parse_mft_file_record,
    parse_usn_records,
)


def make_v2_record(frn: int, parent: int, name: str, attributes: int) -> bytes:
    encoded = name.encode("utf-16-le")
    record_length = 60 + len(encoded)
    record = bytearray(record_length)
    struct.pack_into("<IHH", record, 0, record_length, 2, 0)
    struct.pack_into("<QQ", record, 8, frn, parent)
    struct.pack_into("<I", record, 52, attributes)
    struct.pack_into("<HH", record, 56, len(encoded), 60)
    record[60:] = encoded
    return bytes(record)


class MftParserTests(unittest.TestCase):
    def test_parses_usn_v2_record(self):
        payload = struct.pack("<Q", 99) + make_v2_record(
            42, 5, "Documents", FILE_ATTRIBUTE_DIRECTORY
        )
        records = list(parse_usn_records(payload))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].frn, 42)
        self.assertEqual(records[0].parent_frn, 5)
        self.assertEqual(records[0].name, "Documents")
        self.assertTrue(records[0].is_directory)

    def test_ignores_truncated_record(self):
        payload = struct.pack("<Q", 99) + make_v2_record(42, 5, "x", 0)[:-1]
        self.assertEqual(list(parse_usn_records(payload)), [])

    def test_applies_update_sequence_fixup(self):
        record = bytearray(1024)
        record[0:4] = b"FILE"
        struct.pack_into("<HH", record, 4, 0x30, 3)
        record[0x30:0x32] = b"\xab\xcd"
        record[0x32:0x34] = b"\x11\x22"
        record[0x34:0x36] = b"\x33\x44"
        record[510:512] = b"\xab\xcd"
        record[1022:1024] = b"\xab\xcd"
        self.assertTrue(apply_mft_fixup(record))
        self.assertEqual(record[510:512], b"\x11\x22")
        self.assertEqual(record[1022:1024], b"\x33\x44")

    def test_parse_data_runs_includes_sparse_and_relative_offsets(self):
        # 4 clusters at LCN 8, then 8 sparse clusters, then 2 clusters at LCN 5.
        runs = parse_data_runs(bytes([0x11, 4, 8, 0x01, 8, 0x11, 2, 0xFD, 0]))
        self.assertEqual(runs, [(8, 4), (-1, 8), (5, 2)])

    def test_parses_resident_file_sizes_and_win32_name(self):
        record = make_mft_record(
            pack_file_name("notes.txt", 5, ns=1, logical=12, allocated=16),
            pack_resident_data(12),
        )
        parsed = parse_mft_file_record(record)
        self.assertIsNotNone(parsed)
        base_ref, sequence, is_directory, name, parent, logical, allocated, _modified, _attributes = parsed
        self.assertEqual(base_ref, 0)
        self.assertEqual(sequence, 1)
        self.assertFalse(is_directory)
        self.assertEqual(name, "notes.txt")
        self.assertEqual(parent, 5)
        self.assertEqual((logical, allocated), (12, 0))

    def test_prefers_win32_name_over_dos_name(self):
        record = make_mft_record(
            pack_file_name("NOTES~1.TXT", 5, ns=2),
            pack_file_name("notes.txt", 5, ns=1),
            pack_resident_data(4),
        )
        parsed = parse_mft_file_record(record)
        self.assertEqual(parsed[3], "notes.txt")

    def test_parses_nonresident_data_sizes_and_directory_flag(self):
        file_record = make_mft_record(pack_file_name("big.bin", 5), pack_nonresident_data(100, 128))
        folder = make_mft_record(pack_file_name("Docs", 5, directory=True), flags=0x03)
        unused = make_mft_record(pack_file_name("gone", 5), flags=0)
        self.assertEqual(parse_mft_file_record(file_record)[5:7], (100, 128))
        self.assertTrue(parse_mft_file_record(folder)[2])
        self.assertEqual(parse_mft_file_record(folder)[3], "Docs")
        self.assertIsNone(parse_mft_file_record(unused))

    def test_reads_modified_time_attributes_and_only_keeps_link_reparse_flag(self):
        modified = 116444736000000000 + 1_700_000_000 * 10_000_000
        info = pack_resident_attr(0x10, struct.pack("<4QI", 0, modified, 0, 0, 0x400 | 0x2) + bytes(0x24))
        junction = make_mft_record(info, pack_file_name("link", 5), pack_resident_attr(0xC0, struct.pack("<I", 0xA0000003)))
        onedrive = make_mft_record(info, pack_file_name("cloud.docx", 5), pack_resident_attr(0xC0, struct.pack("<I", 0x9000001A)))
        self.assertEqual(parse_mft_file_record(junction)[7:], (1_700_000_000, 0x402))
        self.assertEqual(parse_mft_file_record(onedrive)[7:], (1_700_000_000, 0x2))


def pack_resident_attr(attr_type: int, value: bytes) -> bytes:
    raw_len = 0x18 + len(value)
    attr_len = (raw_len + 7) & ~7
    buf = bytearray(attr_len)
    struct.pack_into("<II", buf, 0, attr_type, attr_len)
    struct.pack_into("<H", buf, 0x0A, 0x18)
    struct.pack_into("<I", buf, 0x10, len(value))
    struct.pack_into("<H", buf, 0x14, 0x18)
    buf[0x18 : 0x18 + len(value)] = value
    return bytes(buf)


def pack_file_name(
    name: str,
    parent: int,
    ns: int = 1,
    logical: int = 0,
    allocated: int = 0,
    directory: bool = False,
) -> bytes:
    encoded = name.encode("utf-16-le")
    value = bytearray(0x42 + len(encoded))
    struct.pack_into("<Q", value, 0, parent)
    struct.pack_into("<q", value, 0x28, allocated)
    struct.pack_into("<q", value, 0x30, logical)
    if directory:
        struct.pack_into("<I", value, 0x38, 0x10000000)
    value[0x40] = len(name)
    value[0x41] = ns
    value[0x42:] = encoded
    return pack_resident_attr(0x30, bytes(value))


def pack_resident_data(logical: int) -> bytes:
    buf = bytearray(0x18)
    struct.pack_into("<II", buf, 0, 0x80, 0x18)
    struct.pack_into("<I", buf, 0x10, logical)
    struct.pack_into("<H", buf, 0x14, 0x18)
    return bytes(buf)


def pack_nonresident_data(logical: int, allocated: int) -> bytes:
    buf = bytearray(0x40)
    struct.pack_into("<II", buf, 0, 0x80, 0x40)
    buf[8] = 1
    struct.pack_into("<H", buf, 0x20, 0x40)
    struct.pack_into("<q", buf, 0x28, allocated)
    struct.pack_into("<q", buf, 0x30, logical)
    struct.pack_into("<q", buf, 0x38, logical)
    return bytes(buf)


def make_mft_record(*attrs: bytes, flags: int = 1, sequence: int = 1, base: int = 0) -> bytearray:
    record = bytearray(1024)
    record[0:4] = b"FILE"
    struct.pack_into("<H", record, 0x10, sequence)
    struct.pack_into("<H", record, 0x14, 0x38)
    struct.pack_into("<H", record, 0x16, flags)
    struct.pack_into("<I", record, 0x1C, 1024)
    struct.pack_into("<Q", record, 0x20, base)
    pos = 0x38
    for attr in attrs:
        record[pos : pos + len(attr)] = attr
        pos += len(attr)
    struct.pack_into("<I", record, pos, 0xFFFFFFFF)
    struct.pack_into("<I", record, 0x18, pos + 8)
    return record
