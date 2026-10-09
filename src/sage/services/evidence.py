"""Deterministic, evidence-citing answers with no API key or LLM."""

from __future__ import annotations

from ..models import FileRecord, ScanResult


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    size = float(value)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        size /= 1024
        if size < 1024:
            return f"{size:.0f} {unit}" if size >= 100 else f"{size:.1f} {unit}"
    return f"{size:.1f} EB"


def attributes_text(attributes: int) -> str:
    flags = (
        (0x10, "D"),
        (0x01, "R"),
        (0x02, "H"),
        (0x04, "S"),
        (0x20, "A"),
        (0x400, "L"),
        (0x800, "C"),
        (0x4000, "E"),
    )
    return "".join(label for mask, label in flags if attributes & mask) or "—"


class EvidenceAssistant:
    def __init__(self, result: ScanResult | None = None) -> None:
        self.result = result

    def set_result(self, result: ScanResult) -> None:
        self.result = result

    def scan_summary(self) -> str:
        result = self._require_result()
        items, files, folders = result.root_stats
        return (
            f"Scan complete for {result.volume.display_name}.\n\n"
            f"Filesystem: {result.volume.filesystem}\n"
            f"MFT records: {len(result.records):,}\n"
            f"Items: {items:,}\nFiles: {files:,}\nFolders: {folders:,}\n\n"
            + (
                "File sizes were read from $MFT with the hierarchy.\n"
                if result.sizes_complete
                else "Size analysis is running separately so the file tree remains usable.\n"
            )
            + "Evidence source: current native NTFS MFT scan."
        )

    def size_summary(self) -> str:
        result = self._require_result()
        logical, allocated = result.directory_sizes.get(result.root_frn, (0, 0))
        return (
            "Size analysis complete.\n\n"
            f"Logical size: {format_bytes(logical)}\n"
            f"Allocated size: {format_bytes(allocated)}\n"
            f"Unreadable files: {result.size_errors:,}\n\n"
            "Allocated folder totals are sums of contained file allocation; "
            "NTFS directory metadata overhead is not included.\n"
            "Evidence source: current native NTFS MFT scan."
        )

    def answer(self, question: str) -> str:
        result = self._require_result()
        query = " ".join(question.lower().split())
        if not query:
            return "Ask about the scan, file counts, drive usage, or largest folders."
        if "largest folder" in query or "biggest folder" in query:
            return self._largest_folders(result)
        if any(term in query for term in ("space", "size", "storage used", "full")):
            if not result.sizes_complete:
                return (
                    "The MFT hierarchy is ready, but file-size evidence is still being collected. "
                    "I will not estimate an answer without evidence."
                )
            return self.size_summary()
        if any(term in query for term in ("count", "how many", "files", "folders", "scan")):
            return self.scan_summary()
        return (
            "I can currently answer evidence-backed questions about scan counts, used space, "
            "and the largest top-level folders. This basic answer uses scan metadata only; "
            "it cannot determine which files you should delete."
        )

    def _largest_folders(self, result: ScanResult) -> str:
        if not result.sizes_complete:
            return "Largest-folder evidence will be available when background size analysis finishes."
        candidates: list[tuple[int, int, FileRecord]] = []
        for frn in result.children.get(result.root_frn, ()):
            record = result.records.get(frn)
            if record and record.is_directory:
                logical, allocated = result.directory_sizes.get(frn, (0, 0))
                candidates.append((allocated, logical, record))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            return "No top-level folder-size evidence is available."
        lines = [
            f"• {record.name}: {format_bytes(allocated)} allocated ({format_bytes(logical)} logical)"
            for allocated, logical, record in candidates[:10]
        ]
        return (
            f"Largest folders directly under {result.volume.drive}\\:\n\n"
            + "\n".join(lines)
            + "\n\nEvidence source: current native NTFS scan and Windows file metadata."
        )

    def _require_result(self) -> ScanResult:
        if self.result is None:
            raise RuntimeError("Scan an NTFS drive before asking evidence questions.")
        return self.result
