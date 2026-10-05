"""Read-only evidence retrieval over the current NTFS scan."""

from __future__ import annotations

import heapq
import ntpath
import os
import re

from ..models import FileRecord, ScanResult
from .evidence import format_bytes


def assess_path(path: str, system_drive: str | None = None) -> dict[str, str]:
    """Describe consequences, not a guarantee that a filename is disposable."""
    normalized = ntpath.normcase(ntpath.normpath(path.removeprefix("\\\\?\\")))
    drive, tail = ntpath.splitdrive(normalized)
    system = ntpath.normcase((system_drive or os.environ.get("SystemDrive") or "C:").rstrip("\\/"))
    parts = [part for part in tail.split("\\") if part]
    if not drive or not parts:
        return {"risk": "protected", "guidance": "Do not delete or move a drive root."}
    first = parts[0]
    # ponytail: path rules identify likely roles, not installed dependencies.
    # Add verified application/known-folder evidence here when available.
    if ".git" in parts:
        return {"risk": "protected", "guidance": "This is repository metadata. Deleting it can lose local history, branches and uncommitted repository state; moving it alone can break the repository. Keep it with its project."}
    if first == "users" and len(parts) <= 2:
        return {"risk": "protected", "guidance": "This is a user-profile container. Do not delete or move the whole container; inspect its individual documents, downloads and application data instead."}
    if first in {"program files", "program files (x86)", "programdata", "system volume information", "recovery"}:
        action = "Use Windows Installed apps to uninstall an application you no longer use." if first.startswith("program files") else "Use the owning application's management tools; this folder may contain settings, databases or recovery data."
        return {"risk": "protected", "guidance": f"Do not directly delete or move this system/application location. {action}"}
    if drive == system:
        if first in {"windows", "boot"}:
            return {"risk": "protected", "guidance": "Do not directly delete or move this system location. Use Windows Storage cleanup instead."}
        if first in {"pagefile.sys", "swapfile.sys", "hiberfil.sys"}:
            return {"risk": "protected", "guidance": "Windows manages this file; do not delete it directly."}
        if first == "users" and "appdata" in parts:
            if len(parts) >= 6 and parts[2:6] == ["appdata", "local", "pip", "cache"]:
                return {"risk": "managed_cleanup", "guidance": "This matches pip's conventional cache location, not installed packages. Verify the exact cache with `python -m pip cache dir`; if it matches, use `python -m pip cache purge`. Purging discards cached downloads/wheels and may require downloading or rebuilding them later. Do not move the folder manually."}
            if len(parts) >= 5 and parts[2:5] == ["appdata", "local", "temp"]:
                return {"risk": "managed_cleanup", "guidance": "This matches the user temporary-data location. Some files may still be in use. Use Windows Temporary files cleanup for eligible contents; do not delete or relocate the entire Temp folder."}
            return {"risk": "protected", "guidance": "Application data can contain settings and state. Do not delete the folder directly."}
    if "downloads" in parts or "documents" in parts or "desktop" in parts:
        return {"risk": "review", "guidance": "This is in a personal-data location. It is a removal or relocation candidate only if you no longer need this copy and it is not used by an app, shortcut or sync service. The scan does not establish backups or ownership. Removing it loses that copy; moving it changes its path."}
    if ntpath.splitext(normalized)[1] == ".log":
        return {"risk": "unknown", "guidance": "The .log extension suggests a log, but does not establish its owner, retention needs or whether it is in use. Which application created it, and do you need its diagnostic history? Its contents have not been read."}
    return {"risk": "unknown", "guidance": "Its purpose and application dependencies have not been established. Which application created or uses this item? Deletion would remove its data; relocation could break references to its current path. Its contents have not been read."}


class ScanEvidence:
    """Bounded retrieval so a local model never receives the entire drive tree."""

    def __init__(self, result: ScanResult) -> None:
        self.result = result
        self._folders: dict[str, list[int]] | None = None
        self._file_names: dict[str, tuple[int, ...]] = {}
        self._largest_cache: dict[tuple[str, int], tuple[int, ...]] = {}

    def _folder_index(self) -> dict[str, list[int]]:
        if self._folders is None:
            self._folders = {}
            for record in self.result.records.values():
                if record.is_directory and record.frn != self.result.root_frn:
                    self._folders.setdefault(record.name.casefold(), []).append(record.frn)
        return self._folders

    def _file_refs(self, name: str) -> tuple[int, ...]:
        """Cache only requested file names, not hundreds of thousands of entries."""
        if name not in self._file_names:
            if len(self._file_names) >= 128:
                self._file_names.clear()
            self._file_names[name] = tuple(record.frn for record in self.result.records.values()
                                          if not record.is_directory and record.name.casefold() == name)
        return self._file_names[name]

    def mentioned(self, question: str, limit: int = 6) -> dict:
        """Resolve names such as Git before asking the model to interpret them."""
        words = re.findall(r"[\w.\-]+", question.casefold())
        ignored = {"a", "an", "and", "are", "can", "could", "delete", "do", "does", "drive", "file", "files", "folder", "folders", "for", "from", "have", "how", "i", "in", "is", "it", "keep", "me", "move", "my", "of", "on", "or", "please", "remove", "should", "that", "the", "this", "to", "what", "which", "would", "you"}
        ignored.update({"about", "big", "details", "give", "information", "large", "largest", "biggest", "list", "much", "overview", "show", "size", "space", "storage", "tell", "use", "used", "uses", "using", "taking", "take", "worth", "hey", "hello", "hi", "these", "those", "them", "its", "here", "make", "free", "up", "safe", "unsafe", "important", "consist", "consuming", "itself", "relocate", "doing", "contains", "contain", "purpose", "explain", "item", "items", "some"})
        index = self._folder_index()
        found: list[int] = []
        quoted = [value.casefold() for groups in re.findall(r'"([^"\n]+)"|`([^`\n]+)`|(?<!\w)\x27([^\x27\n]+)\x27', question) for value in groups if value]
        for name in quoted:
            for frn in index.get(name, ()):
                if frn not in found and len(found) < limit:
                    found.append(frn)
        for width in range(min(5, len(words)), 0, -1):
            if len(found) >= limit:
                break
            for start in range(len(words) - width + 1):
                phrase = " ".join(words[start:start + width]).rstrip(".")
                if width == 1 and phrase in ignored:
                    continue
                for frn in index.get(phrase, ()):
                    if frn not in found:
                        found.append(frn)
                        if len(found) >= limit:
                            break
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
        terms = list(dict.fromkeys(quoted + [word.rstrip(".") for word in words if word not in ignored and len(word.rstrip(".")) >= 2]))[:4]
        if found:
            for name in {self.result.records[frn].name.casefold() for frn in found}:
                found.extend(frn for frn in self._file_refs(name) if frn not in found)
            found = found[:limit]
        match_type = "exact"
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
            match_type = "exact" if exact and not partial else "partial"
        return {"items": [self._entry(self.result.records[frn]) for frn in found], "searched_terms": terms, "match_type": match_type}

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
            "cleanup_risk": ({"risk": "protected", "guidance": "This item is a filesystem link/reparse point or is marked system. Its target/role must be verified before cleanup or relocation."}
                             if record.attributes & (0x400 | 0x4) else assess_path(path or "")),
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
        if requested == ntpath.normcase(result.volume.drive):
            requested += "\\"
        if not requested.startswith(ntpath.normcase(result.volume.drive + "\\")):
            return {"error": "Path is outside the current scanned drive."}
        if requested == ntpath.normcase(result.volume.drive + "\\"):
            logical, allocated = result.directory_sizes.get(result.root_frn, (0, 0))
            return {"frn": result.root_frn, "path": result.volume.drive + "\\", "kind": "drive", "logical_bytes": logical if result.sizes_complete else None, "allocated_bytes": allocated if result.sizes_complete else None, "logical_display": format_bytes(logical) if result.sizes_complete else "pending", "allocated_display": format_bytes(allocated) if result.sizes_complete else "pending", "cleanup_risk": assess_path(requested)}
        name = ntpath.basename(requested)
        for frn in tuple(self._folder_index().get(name.casefold(), ())) + self._file_refs(name.casefold()):
            record = result.records[frn]
            actual = result.path_for(record.frn)
            if actual and ntpath.normcase(actual) == requested:
                return self._entry(record)
        return {"error": "Path was not found in the current scan.", "path": path}

    def scoped_items(self, path: str, limit: int = 8) -> dict:
        """Direct children only: do not mistake a tiny nested component for a candidate."""
        parent = self.inspect(path)
        if "error" in parent:
            return parent
        records = [self.result.records[frn] for frn in self.result.children.get(parent["frn"], ())]
        biggest = heapq.nlargest(limit, records, key=lambda record: self.result.directory_sizes.get(record.frn, (0, 0))[1] if record.is_directory else record.allocated_size)
        return {"items": [self._entry(record) for record in biggest], "total_children": len(records)}

    def cleanup_candidates(self, path: str, limit: int = 6) -> dict:
        """Rank conditional candidates in the requested subtree, not the whole drive."""
        parent = self.inspect(path)
        if "error" in parent:
            return parent
        if not self.result.sizes_complete:
            return {"status": "sizes_pending", "items": []}
        def candidates():
            pending = [(parent["frn"], parent["path"])]
            while pending:
                frn, current_path = pending.pop()
                record = self.result.records[frn]
                risk = assess_path(current_path)["risk"]
                if record.attributes & (0x400 | 0x4):
                    continue
                if frn != parent["frn"] and risk in {"managed_cleanup", "review"}:
                    # Whole personal folders are not assumed disposable. Rank files,
                    # plus recognized cache roots; never count a parent and its children.
                    if not record.is_directory or risk == "managed_cleanup":
                        entry = self._entry(record)
                        if entry["allocated_bytes"]:
                            yield entry
                        continue
                if record.is_directory:
                    parts = ntpath.normcase(current_path).split("\\")
                    if ".git" in parts or (len(parts) == 2 and parts[1] in {"windows", "program files", "program files (x86)", "programdata", "recovery", "system volume information"}):
                        continue
                    if "appdata" in parts and "programs" in parts:
                        continue
                    if "appdata" in parts and risk == "protected":
                        index = parts.index("appdata")
                        suffix = parts[index + 1:]
                        # No supported cleanup rule for other application subtrees.
                        if suffix and suffix not in (["local"], ["local", "pip"]):
                            continue
                    pending.extend((child, ntpath.join(current_path, self.result.records[child].name)) for child in self.result.children.get(frn, ()))
        ranked = heapq.nlargest(limit, candidates(), key=lambda item: item["allocated_bytes"])
        return {"status": "complete", "items": ranked, "scope": parent["path"], "size_errors": self.result.size_errors}
