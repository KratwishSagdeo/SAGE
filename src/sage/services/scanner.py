"""Public scan orchestration for the fast MFT phase and size phase."""

from __future__ import annotations

from collections.abc import Callable

from ..models import ScanResult
from ..platform.windows.drives import get_volume_info
from ..platform.windows.mft import enumerate_mft
from ..platform.windows.sizes import collect_file_sizes
from .hierarchy import build_scan_result, calculate_directory_sizes


def scan_ntfs(
    drive: str,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ScanResult:
    """Complete only the fast MFT hierarchy phase."""
    volume = get_volume_info(drive)
    if volume.filesystem.upper() != "NTFS":
        raise ValueError(f"{volume.drive} is {volume.filesystem}, not NTFS.")
    records, skipped = enumerate_mft(
        volume.drive,
        progress=progress,
        cancelled=cancelled,
    )
    stage = getattr(progress, "on_stage", None)
    if stage:
        stage("Building MFT hierarchy")
    result = build_scan_result(volume, records, skipped)
    files = [record for record in result.records.values() if not record.is_directory]
    if all(record.size_ready for record in files):
        calculate_directory_sizes(result)
        result.sized_files = len(files)
    return result


def analyze_sizes(
    result: ScanResult,
    progress: Callable[[int, int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Aggregate folder totals; walk directories only when MFT sizes are missing."""
    files = [record for record in result.records.values() if not record.is_directory]
    if not files or all(record.size_ready for record in files):
        result.sized_files = len(files)
        calculate_directory_sizes(result)
        if progress:
            progress(len(files), len(files), result.size_errors)
        return
    collect_file_sizes(result, progress, cancelled)
    if cancelled and cancelled():
        raise InterruptedError("Size analysis cancelled.")
    calculate_directory_sizes(result)
