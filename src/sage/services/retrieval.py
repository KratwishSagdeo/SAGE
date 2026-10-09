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
    # ponytail: path rules identify likely roles, not installed dependencies.
    # Add verified application/known-folder evidence here when available.
    if ".git" in parts or first == "git" or "git" in parts:
        return {"risk": "protected", "guidance": "This is repository metadata. Deleting it can lose local history, branches and uncommitted repository state; moving it alone can break the repository. Keep it with its project."}
    if first == "users" and len(parts) <= 2:
        return {"risk": "protected", "guidance": "This is a user-profile container. Do not delete or move the whole container; inspect its individual documents, downloads and application data instead."}
    if first in {"program files", "program files (x86)", "programdata", "system volume information", "recovery"}:
        action = "Use Windows Installed apps to uninstall an application you no longer use." if first.startswith("program files") else "Use Windows Storage cleanup or the relevant application settings."
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
        return {"risk": "review", "guidance": "Personal files require your review and a backup check before removal."}
    if ntpath.splitext(normalized)[1] == ".log":
        return {"risk": "unknown", "guidance": "The .log extension suggests a log, but does not establish its owner, retention needs or whether it is in use. Which application created it, and do you need its diagnostic history? Its contents have not been read."}
    return {"risk": "unknown", "guidance": "The scan alone cannot establish that deleting or moving this path is safe."}


class ScanEvidence:
    """Bounded retrieval so a local model never receives the entire drive tree."""

    def __init__(self, result: ScanResult) -> None:
        self.result = result
        self._folders: dict[str, list[int]] | None = None
        self._largest_cache: dict[tuple, tuple[int, ...]] = {}
        self._name_cache: dict[tuple, tuple[int, ...]] = {}
        self._summary: dict | None = None

    _IGNORED = frozenset({
        "a", "an", "and", "are", "can", "could", "delete", "do", "does", "drive", "file", "files", "folder",
        "folders", "for", "from", "have", "how", "i", "in", "is", "it", "keep", "me", "move", "my", "of", "on",
        "or", "please", "remove", "should", "that", "the", "this", "to", "what", "which", "would", "you",
        "about", "big", "details", "give", "information", "large", "largest", "biggest", "list", "much",
        "overview", "show", "size", "space", "storage", "tell", "use", "used", "uses", "using", "taking",
        "take", "worth", "hey", "hello", "hi", "these", "those", "them", "its", "itself", "safe", "free", "clear",
        "clean", "cleanup", "junk", "inside", "not", "avoid", "never", "shouldn", "t", "explain", "really", "so", "needed", "necessary", "sensitive", "private", "contains", "contain", "why", "where", "there", "here", "c", "d",
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
        exact_match = bool(found)
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
            found = exact[:limit] if exact else partial[:limit]
            exact_match = bool(exact)
        match_type = "exact" if exact_match else ("partial" if found else "none")
        return {"items": [self._entry(self.result.records[frn]) for frn in found], "searched_terms": terms,
                "exact": exact_match, "match_type": match_type}

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
            "role_basis": "Path/attribute heuristic",
            "owner_verified": False,
            "dependencies_verified": False,
            "is_reparse_point": bool(record.attributes & 0x400),
            "child_names": [result.records[frn].name for frn in result.children.get(record.frn, ())[:6]] if record.is_directory else [],
            "child_count": len(result.children.get(record.frn, ())) if record.is_directory else 0,
        }

    def largest(self, kind: str = "folders", limit: int = 8, *, scope: str | None = None, protected_only: bool = False) -> dict:
        result = self.result
        limit = max(1, min(int(limit), 20))
        scope = scope or result.volume.drive + "\\"
        parent = self.inspect(scope)
        if "error" in parent:
            return parent
        if not result.sizes_complete:
            return {"status": "sizes_pending", "items": []}
        def candidates():
            pending = list(result.children.get(parent["frn"], ())) if not parent.get("is_reparse_point") else []
            seen = {parent["frn"]}
            while pending:
                frn = pending.pop()
                if frn in seen:
                    continue
                seen.add(frn)
                record = result.records[frn]
                # Do not rank or traverse junctions as though they were ordinary folders.
                if record.attributes & 0x400:
                    continue
                matches = kind == "items" or record.is_directory == (kind != "files")
                if matches and (record.is_directory or (record.size_ready and not record.size_error)):
                    if not protected_only or assess_path(result.path_for(frn) or "")["risk"] == "protected":
                        yield record
                if record.is_directory:
                    pending.extend(result.children.get(frn, ()))
        # ponytail: first scoped rank walks its subtree; keep 32 ranks per completed scan.
        # If real scans make this too slow, precompute ranks during scan completion.
        cache_key = (kind, limit, ntpath.normcase(scope), protected_only)
        if cache_key not in self._largest_cache:
            if len(self._largest_cache) >= 32:
                self._largest_cache.clear()
            self._largest_cache[cache_key] = tuple(record.frn for record in heapq.nlargest(
                limit, candidates(), key=self._allocated,
            ))
        return {"status": "complete", "scope": scope, "ranking": "allocated_bytes",
                "items": [self._entry(result.records[frn]) for frn in self._largest_cache[cache_key]]}

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
            return {"frn": result.root_frn, "path": result.volume.drive + "\\", "kind": "drive", "logical_bytes": logical if result.sizes_complete else None, "allocated_bytes": allocated if result.sizes_complete else None, "logical_display": format_bytes(logical) if result.sizes_complete else "pending", "allocated_display": format_bytes(allocated) if result.sizes_complete else "pending", "cleanup_risk": assess_path(path)}
        current = result.root_frn
        for part in requested[len(result.volume.drive) + 1:].split("\\"):
            current = next(
                (frn for frn in result.children.get(current, ())
                 if frn in result.records and ntpath.normcase(result.records[frn].name) == part),
                None,
            )
            if current is None:
                break
        if current is not None:
            return self._entry(result.records[current])
        name = ntpath.basename(requested)
        for record in result.records.values():
            if record.name.casefold() == name.casefold():
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
                        if suffix and suffix not in (["local"], ["local", "pip"]):
                            continue
                    pending.extend((child, ntpath.join(current_path, self.result.records[child].name)) for child in self.result.children.get(frn, ()))
        ranked = heapq.nlargest(limit, candidates(), key=lambda item: item["allocated_bytes"])
        return {"status": "complete", "items": ranked, "scope": parent["path"], "size_errors": self.result.size_errors}

    @staticmethod
    def within(path: str, scope: str) -> bool:
        path = ntpath.normcase(ntpath.normpath(path))
        scope = ntpath.normcase(ntpath.normpath(scope)).rstrip("\\")
        return path == scope or path.startswith(scope + "\\")

    def resolve_name(self, name: str, scope: str | None = None, limit: int = 7) -> list[dict]:
        """Exact names only; a partial match is a clarification option, not a target."""
        key = (name.casefold(), ntpath.normcase(scope or ""), limit)
        if key not in self._name_cache:
            matches = []
            for record in self.result.records.values():
                if record.name.casefold() != key[0] or record.frn == self.result.root_frn:
                    continue
                path = self.result.path_for(record.frn)
                if path and (scope is None or self.within(path, scope)):
                    matches.append(record.frn)
                    if len(matches) >= limit:
                        break
            # ponytail: exact-name lookup is one metadata pass on a cache miss.
            # Cache 32 queries rather than building a second index for every file.
            if len(self._name_cache) >= 32:
                self._name_cache.clear()
            self._name_cache[key] = tuple(matches)
        return [self._entry(self.result.records[frn]) for frn in self._name_cache[key]]

    def folder_summary(self, path: str) -> dict:
        parent = self.inspect(path)
        if "error" in parent:
            return parent
        if parent.get("is_reparse_point"):
            return {"path": path, "coverage": "Reparse point; linked contents were not inspected."}
        children = [self.result.records[frn] for frn in self.result.children.get(parent["frn"], ())]
        types: dict[str, dict] = {}
        files = [record for record in children if not record.is_directory]
        for record in files:
            extension = ntpath.splitext(record.name)[1].casefold() or "(no extension)"
            group = types.setdefault(extension, {"extension": extension, "files": 0, "measured_files": 0, "allocated_bytes": 0})
            group["files"] += 1
            if record.size_ready and not record.size_error:
                group["measured_files"] += 1
                group["allocated_bytes"] += record.allocated_size
        groups = heapq.nlargest(4, types.values(), key=lambda item: item["allocated_bytes"])
        for group in groups:
            group["allocated_display"] = format_bytes(group.pop("allocated_bytes")) if self.result.sizes_complete else "pending"
        return {
            "path": path, "direct_files": len(files), "direct_folders": len(children) - len(files),
            "subtree_counts": dict(zip(("items", "files", "folders"), self.result.stats.get(parent["frn"], (0, 0, 0)))),
            "largest_direct_children": [self._entry(child) for child in heapq.nlargest(6, children, key=self._allocated)],
            "direct_file_types": groups,
            "coverage": "Counts cover indexed direct children; largest-child and file-type examples are partial. No contents were read.",
        }
