"""Read-only evidence retrieval over the current NTFS scan."""

from __future__ import annotations

import heapq
import ntpath
import os
import re

from ..models import FileRecord, ScanResult
from .evidence import format_bytes

_WORD = re.compile(r"[\w.\-]+")


def _name_key(name: str) -> str:
    """Match 'Program Files (x86)' to the words 'program files x86'."""
    return " ".join(_WORD.findall(name.casefold()))


def assess_path(path: str, system_drive: str | None = None) -> dict[str, str]:
    """Classify cleanup risk conservatively; never claim a path is safe to delete."""
    normalized = ntpath.normcase(ntpath.normpath(path.removeprefix("\\\\?\\")))
    drive, tail = ntpath.splitdrive(normalized)
    system = ntpath.normcase((system_drive or os.environ.get("SystemDrive") or "C:").rstrip("\\/"))
    parts = [part for part in tail.split("\\") if part]
    if not drive or not parts:
        return {"risk": "protected", "guidance": "Do not delete or move a drive root."}
    first = parts[0]
    if first in {"program files", "program files (x86)", "programdata", "system volume information", "recovery"}:
        action = "Uninstall an unwanted application through Windows Settings." if first.startswith("program files") else "Use Windows Storage cleanup or the relevant application settings."
        return {"risk": "protected", "guidance": f"Do not directly delete or move this system/application location. {action}"}
    if drive == system:
        if first in {"windows", "boot"}:
            return {"risk": "protected", "guidance": "Do not directly delete or move this system location. Use Windows Storage cleanup instead."}
        if first in {"pagefile.sys", "swapfile.sys", "hiberfil.sys"}:
            return {"risk": "protected", "guidance": "Windows manages this file; do not delete it directly."}
        if first == "users" and "appdata" in parts:
            return {"risk": "protected", "guidance": "Application data can contain settings and state. Do not delete the folder directly."}
    if "downloads" in parts or "documents" in parts or "desktop" in parts:
        return {"risk": "review", "guidance": "Personal files require your review and a backup check before removal."}
    return {"risk": "unknown", "guidance": "The scan alone cannot establish that deleting or moving this path is safe."}


class ScanEvidence:
    """Bounded retrieval so a local model never receives the entire drive tree."""

    def __init__(self, result: ScanResult) -> None:
        self.result = result
        self._folders: dict[str, list[int]] | None = None
        self._largest_cache: dict[tuple[str, int], tuple[int, ...]] = {}
        self._summary: dict | None = None

    _IGNORED = frozenset({
        "a", "an", "and", "are", "can", "could", "delete", "do", "does", "drive", "file", "files", "folder",
        "folders", "for", "from", "have", "how", "i", "in", "is", "it", "keep", "me", "move", "my", "of", "on",
        "or", "please", "remove", "should", "that", "the", "this", "to", "what", "which", "would", "you",
        "about", "big", "details", "give", "information", "large", "largest", "biggest", "list", "much",
        "overview", "show", "size", "space", "storage", "tell", "use", "used", "uses", "using", "taking",
        "take", "worth", "hey", "hello", "hi", "these", "those", "them", "its", "safe", "free", "clear",
        "clean", "cleanup", "junk", "inside", "contains", "contain", "why", "where", "there", "here", "c", "d",
    })

    def _folder_index(self) -> dict[str, list[int]]:
        if self._folders is None:
            self._folders = {}
            for record in self.result.records.values():
                if record.is_directory and record.frn != self.result.root_frn:
                    self._folders.setdefault(_name_key(record.name), []).append(record.frn)
        return self._folders

    def named_folders(self, question: str, limit: int = 6) -> list[int]:
        """Folders named exactly in the question; longest names win over their sub-phrases."""
        words = _WORD.findall(question.casefold())
        index = self._folder_index()
        found: list[int] = []
        quoted = [value for groups in re.findall(r'"([^"\n]+)"|`([^`\n]+)`|(?<!\w)\x27([^\x27\n]+)\x27', question) for value in groups if value]
        for name in quoted:
            for frn in index.get(_name_key(name), ()):
                if frn not in found and len(found) < limit:
                    found.append(frn)
        used: set[int] = set()
        for width in range(min(5, len(words)), 0, -1):
            for start in range(len(words) - width + 1):
                span = range(start, start + width)
                if len(found) >= limit:
                    return found
                if used.intersection(span):
                    continue
                phrase = " ".join(words[start:start + width]).rstrip(".")
                if width == 1 and phrase in self._IGNORED:
                    continue
                matches = [frn for frn in index.get(phrase, ()) if frn not in found]
                if matches:
                    used.update(span)
                    found.extend(matches[: limit - len(found)])
        return found

    def mentioned(self, question: str, limit: int = 6) -> dict:
        """Resolve names such as Git before asking the model to interpret them."""
        words = _WORD.findall(question.casefold())
        quoted = [value.casefold() for groups in re.findall(r'"([^"\n]+)"|`([^`\n]+)`|(?<!\w)\x27([^\x27\n]+)\x27', question) for value in groups if value]
        found = self.named_folders(question, limit)
        ignored = self._IGNORED
        terms = list(dict.fromkeys(quoted + [word.rstrip(".") for word in words if word not in ignored and len(word.rstrip(".")) >= 2]))[:4]
        if not found and terms:
            exact: list[int] = []
            partial: list[int] = []
            for record in self.result.records.values():
                name = record.name.casefold()
                if name in terms:
                    exact.append(record.frn)
                elif len(partial) < limit and any(term in name for term in terms):
                    partial.append(record.frn)
            found = (exact + partial)[:limit]
        return {"items": [self._entry(self.result.records[frn]) for frn in found], "searched_terms": terms}

    def _allocated(self, record: FileRecord) -> int:
        if record.is_directory:
            return self.result.directory_sizes.get(record.frn, (0, 0))[1]
        return record.allocated_size

    def largest_children(self, frn: int, limit: int = 8) -> list[str]:
        """'path — size' lines for a folder's biggest direct children."""
        result = self.result
        children = [result.records[c] for c in result.children.get(frn, ()) if c in result.records]
        biggest = heapq.nlargest(limit, children, key=self._allocated)
        return [
            f"{result.path_for(child.frn)} ({'folder' if child.is_directory else 'file'}) — {format_bytes(self._allocated(child))}"
            for child in biggest
        ]

    def drive_summary(self) -> dict:
        """Compact whole-drive picture sent with every model request."""
        if self._summary is None:
            result = self.result
            items, files, folders = result.root_stats
            logical, allocated = result.directory_sizes.get(result.root_frn, (0, 0))
            summary = {
                "drive": result.volume.display_name,
                "used": format_bytes(allocated) if result.sizes_complete else "pending",
                "files": files,
                "folders": folders,
                "top_level_by_size": self.largest_children(result.root_frn, 12),
            }
            if result.sizes_complete:
                summary["largest_files"] = [
                    f"{entry['path']} — {entry['allocated_display']}"
                    for entry in self.largest("files", 10)["items"]
                ]
                deep = heapq.nlargest(
                    40,
                    (r for r in result.records.values() if r.is_directory and r.frn != result.root_frn),
                    key=self._allocated,
                )
                leaves = [r for r in deep if not any(
                    other is not r and self._is_ancestor(r.frn, other.frn) for other in deep
                )]
                summary["biggest_leaf_folders"] = [
                    f"{result.path_for(r.frn)} — {format_bytes(self._allocated(r))}" for r in leaves[:10]
                ]
            self._summary = summary
        return self._summary

    def _is_ancestor(self, ancestor: int, frn: int) -> bool:
        records = self.result.records
        current = records[frn].parent_frn
        for _ in range(64):
            if current == ancestor:
                return True
            if current == self.result.root_frn or current not in records:
                return False
            current = records[current].parent_frn
        return False

    def overview(self) -> dict:
        result = self.result
        items, files, folders = result.root_stats
        logical, allocated = result.directory_sizes.get(result.root_frn, (0, 0))
        return {
            "drive": result.volume.drive,
            "filesystem": result.volume.filesystem,
            "records": len(result.records),
            "items": items,
            "files": files,
            "folders": folders,
            "sizes_complete": result.sizes_complete,
            "sized_files": result.sized_files,
            "logical_bytes": logical if result.sizes_complete else None,
            "allocated_bytes": allocated if result.sizes_complete else None,
            "size_errors": result.size_errors,
        }

    def _entry(self, record: FileRecord) -> dict:
        result = self.result
        path = result.path_for(record.frn)
        logical, allocated = (
            result.directory_sizes.get(record.frn, (0, 0))
            if record.is_directory else (record.logical_size, record.allocated_size)
        )
        ready = result.sizes_complete if record.is_directory else record.size_ready and not record.size_error
        return {
            "frn": record.frn,
            "path": path,
            "kind": "folder" if record.is_directory else "file",
            "logical_bytes": logical if ready else None,
            "allocated_bytes": allocated if ready else None,
            "logical_display": format_bytes(logical) if ready else "pending",
            "allocated_display": format_bytes(allocated) if ready else "pending",
            "size_error": record.size_error,
            "cleanup_risk": assess_path(path or ""),
            "child_names": [result.records[frn].name for frn in result.children.get(record.frn, ())[:6]] if record.is_directory else [],
            "child_count": len(result.children.get(record.frn, ())) if record.is_directory else 0,
        }

    def largest(self, kind: str = "folders", limit: int = 8) -> dict:
        result = self.result
        limit = max(1, min(int(limit), 20))
        if not result.sizes_complete:
            return {"status": "sizes_pending", "items": []}
        want_folders = kind != "files"
        candidates = (
            record for record in result.records.values()
            if record.frn != result.root_frn and record.is_directory == want_folders
        )
        cache_key = ("folders" if want_folders else "files", limit)
        if cache_key not in self._largest_cache:
            self._largest_cache[cache_key] = tuple(record.frn for record in heapq.nlargest(
                limit, candidates,
                key=lambda record: result.directory_sizes.get(record.frn, (0, 0))[1]
                if want_folders else record.allocated_size,
            ))
        biggest = [result.records[frn] for frn in self._largest_cache[cache_key]]
        return {"status": "complete", "items": [self._entry(record) for record in biggest]}

    def search(self, term: str, limit: int = 10) -> dict:
        term = term.strip().casefold()[:120]
        limit = max(1, min(int(limit), 20))
        if len(term) < 2:
            return {"error": "Provide at least two characters to search."}
        matches = []
        for record in self.result.records.values():
            if term in record.name.casefold():
                matches.append(record)
                if len(matches) >= limit:
                    break
        return {"term": term, "items": [self._entry(record) for record in matches]}

    def inspect(self, path: str) -> dict:
        result = self.result
        requested = ntpath.normcase(ntpath.normpath(path.removeprefix("\\\\?\\")))
        if not requested.startswith(ntpath.normcase(result.volume.drive + "\\")):
            return {"error": "Path is outside the current scanned drive."}
        if requested == ntpath.normcase(result.volume.drive + "\\"):
            logical, allocated = result.directory_sizes.get(result.root_frn, (0, 0))
            return {"path": path, "kind": "drive", "logical_bytes": logical if result.sizes_complete else None, "allocated_bytes": allocated if result.sizes_complete else None, "logical_display": format_bytes(logical) if result.sizes_complete else "pending", "allocated_display": format_bytes(allocated) if result.sizes_complete else "pending", "cleanup_risk": assess_path(path)}
        current = result.root_frn
        for part in requested[len(result.volume.drive) + 1:].split("\\"):
            current = next(
                (frn for frn in result.children.get(current, ())
                 if frn in result.records and ntpath.normcase(result.records[frn].name) == part),
                None,
            )
            if current is None:
                return {"error": "Path was not found in the current scan.", "path": path}
        return self._entry(result.records[current])
