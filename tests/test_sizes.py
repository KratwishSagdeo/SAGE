import ctypes
import struct
import unittest
from unittest.mock import patch

from sage.config import FILE_ATTRIBUTE_DIRECTORY
from sage.models import FileRecord, VolumeInfo
from sage.platform.windows.sizes import (
    FILE_ID_BOTH_DIR_INFO_HEADER,
    _directory_entries,
    collect_file_sizes,
)
from sage.services.hierarchy import build_scan_result
from sage.services.scanner import analyze_sizes, scan_ntfs


class SizeCollectionTests(unittest.TestCase):
    def test_updates_file_and_folder_sizes_while_collecting(self):
        records = {
            5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY),
            10: FileRecord(10, 5, "Data", FILE_ATTRIBUTE_DIRECTORY),
            11: FileRecord(11, 10, "a.bin", 0),
            12: FileRecord(12, 10, "b.bin", 0),
        }
        result = build_scan_result(VolumeInfo("E:", "Data", "NTFS"), records)
        observed = []
        def directory_sizes(path):
            return [(11, 5, 8), (12, 3, 4)] if path == "E:\\Data" else []

        with patch("sage.platform.windows.sizes.get_directory_file_sizes", side_effect=directory_sizes):
            with patch("sage.platform.windows.sizes.get_file_sizes") as per_file:
                with patch("sage.platform.windows.sizes.SIZE_PROGRESS_INTERVAL", 1):
                    collect_file_sizes(
                        result,
                        progress=lambda current, total, errors: observed.append(
                            (current, total, errors, result.directory_sizes.get(5, (0, 0)))
                        ),
                    )
                per_file.assert_not_called()
        self.assertEqual(result.directory_sizes[5], (8, 12))
        self.assertEqual(result.directory_sizes[10], (8, 12))
        self.assertEqual(result.sized_files, 2)
        self.assertTrue(all(record.size_ready for record in (records[11], records[12])))
        self.assertEqual(observed[-1], (2, 2, 0, (8, 12)))

    def test_unlisted_file_uses_exact_per_file_fallback(self):
        records = {
            5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY),
            11: FileRecord(11, 5, "a.bin", 0),
        }
        result = build_scan_result(VolumeInfo("E:", "Data", "NTFS"), records)
        with patch("sage.platform.windows.sizes.get_directory_file_sizes", return_value=[]):
            with patch("sage.platform.windows.sizes.get_file_sizes", return_value=(5, 8)) as per_file:
                collect_file_sizes(result)
        per_file.assert_called_once_with("E:\\a.bin")
        self.assertEqual((records[11].logical_size, records[11].allocated_size), (5, 8))

    def test_directory_buffer_extracts_file_id_and_sizes(self):
        size = ctypes.sizeof(FILE_ID_BOTH_DIR_INFO_HEADER)
        buffer = ctypes.create_string_buffer(size + 8)
        struct.pack_into("<q", buffer, FILE_ID_BOTH_DIR_INFO_HEADER.EndOfFile.offset, 5)
        struct.pack_into("<q", buffer, FILE_ID_BOTH_DIR_INFO_HEADER.AllocationSize.offset, 8)
        struct.pack_into("<I", buffer, FILE_ID_BOTH_DIR_INFO_HEADER.FileNameLength.offset, 2)
        struct.pack_into("<Q", buffer, FILE_ID_BOTH_DIR_INFO_HEADER.FileId.offset, 11)
        buffer[size:size + 2] = "a".encode("utf-16-le")
        self.assertEqual(_directory_entries(buffer), [(11, 5, 8)])

    def test_analyze_sizes_skips_directory_walk_when_mft_already_sized(self):
        records = {
            5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY, size_ready=True),
            11: FileRecord(11, 5, "a.bin", 0, 5, 8, size_ready=True),
        }
        result = build_scan_result(VolumeInfo("E:", "Data", "NTFS"), records)
        with patch("sage.services.scanner.collect_file_sizes") as collect:
            analyze_sizes(result)
        collect.assert_not_called()
        self.assertTrue(result.sizes_complete)
        self.assertEqual(result.directory_sizes[5], (5, 8))
        self.assertEqual(result.sized_files, 1)

    def test_scan_ntfs_completes_sizes_from_mft_records(self):
        records = {
            5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY, size_ready=True),
            11: FileRecord(11, 5, "a.bin", 0, 5, 8, size_ready=True),
        }

        def fake_enumerate(_drive, progress, cancelled):
            if progress:
                progress(1)
            return records, 0

        with patch(
            "sage.services.scanner.get_volume_info",
            return_value=VolumeInfo("E:", "Data", "NTFS"),
        ), patch("sage.services.scanner.enumerate_mft", side_effect=fake_enumerate):
            result = scan_ntfs("E:")
        self.assertTrue(result.sizes_complete)
        self.assertEqual(result.directory_sizes[5], (5, 8))


if __name__ == "__main__":
    unittest.main()
