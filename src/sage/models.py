"""Domain models shared by the scanner, evidence engine, and UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import FILE_ATTRIBUTE_DIRECTORY


@dataclass(slots=True)
class FileRecord:
    """The minimum useful evidence retained for one MFT record."""

    frn: int
    parent_frn: int
    name: str
    attributes: int
    logical_size: int = 0
    allocated_size: int = 0
    size_error: str | None = None
    size_ready: bool = False
    modified: int = 0  # Unix seconds from $STANDARD_INFORMATION; 0 when unknown.

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)


@dataclass(frozen=True, slots=True)
class VolumeInfo:
    drive: str
    label: str
    filesystem: str

    @property
    def display_name(self) -> str:
        suffix = f" {self.label}" if self.label else ""
        return f"{self.drive}{suffix}"


@dataclass(slots=True)
class ScanResult:
    volume: VolumeInfo
    records: dict[int, FileRecord]
    children: dict[int, list[int]]
    root_frn: int
    stats: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    directory_sizes: dict[int, tuple[int, int]] = field(default_factory=dict)
    skipped_records: int = 0
    size_errors: int = 0
    sized_files: int = 0
    sizes_complete: bool = False
    scanned_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def path_for(self, frn: int) -> str | None:
        """Build a path without following the filesystem recursively."""
        if frn == self.root_frn:
            return self.volume.drive + "\\"

        parts: list[str] = []
        seen: set[int] = set()
        current = frn
        while current != self.root_frn:
            if current in seen:
                return None
            seen.add(current)
            record = self.records.get(current)
            if record is None:
                return None
            parts.append(record.name)
            current = record.parent_frn

        parts.reverse()
        return self.volume.drive + "\\" + "\\".join(parts)

    @property
    def root_stats(self) -> tuple[int, int, int]:
        return self.stats.get(self.root_frn, (0, 0, 0))
