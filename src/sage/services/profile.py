"""Per-item evidence profile: scan metadata plus owner and origin facts read on demand."""

from __future__ import annotations

import ctypes
import heapq
import ntpath
import os
import struct
import time
from collections.abc import Callable
from datetime import datetime, timezone

from ..models import FileRecord, ScanResult
from .evidence import format_bytes
from .known_folders import describe

_DAY = 86400
_WALK_LIMIT = 400_000
_ATTRIBUTE_NAMES = (
    (0x1, "read-only"), (0x2, "hidden"), (0x4, "system"), (0x400, "link (junction/symlink)"),
    (0x800, "compressed"), (0x200, "sparse"), (0x4000, "encrypted"), (0x1000, "offline (cloud)"),
    (0x80000, "pinned (always on this device)"), (0x100000, "online-only (cloud)"),
    (0x400000, "cloud file downloaded on demand"),
)
_MARKERS = {
    "package.json": "Node.js project", ".git": "Git repository", "pyproject.toml": "Python project",
    "requirements.txt": "Python project", "cargo.toml": "Rust project", "pom.xml": "Maven project",
    "build.gradle": "Gradle project", "build.gradle.kts": "Gradle project", "go.mod": "Go project",
    "steam.exe": "Steam client", "steamapps": "Steam game library", "unins000.exe": "installed app with an uninstaller",
    "uninstall.exe": "installed app with an uninstaller", "projectsettings": "Unity project",
    "appxmanifest.xml": "Microsoft Store app package",
}
_CONTAINERS = frozenset({"program files", "program files (x86)", "programdata", "users", "windows"})
_MARKER_SUFFIXES = {".sln": "Visual Studio solution", ".csproj": ".NET project", ".vhdx": "virtual disk image",
                    ".vmdk": "virtual disk image", ".iso": "disk image"}


def _date(seconds: int | float) -> str | None:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d") if seconds else None


def attribute_names(attributes: int) -> list[str]:
    return [name for flag, name in _ATTRIBUTE_NAMES if attributes & flag]


def installed_apps() -> list[dict]:
    """Programs from the Windows uninstall registry, with the folder each one lives in."""
    try:
        import winreg
    except ImportError:
        return []
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    views = ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
             (winreg.HKEY_CURRENT_USER, 0))
    apps: dict[str, dict] = {}
    for hive, view in views:
        try:
            root = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                        values = {}
                        for value_index in range(winreg.QueryInfoKey(key)[1]):
                            name, value, _kind = winreg.EnumValue(key, value_index)
                            values[name] = value
                except OSError:
                    continue
                name = values.get("DisplayName")
                if not name or values.get("SystemComponent") == 1:
                    continue
                location = str(values.get("InstallLocation") or "").strip().strip('"')
                if not location:
                    icon = str(values.get("DisplayIcon") or "").split(",")[0].strip().strip('"')
                    location = ntpath.dirname(icon) if icon.lower().endswith(".exe") else ""
                folded = ntpath.normcase(ntpath.normpath(location)) if location else ""
                parts = [part for part in folded.split("\\") if part]
                if (len(parts) < 2 or (len(parts) == 2 and parts[1] in _CONTAINERS)
                        or "\\windows\\installer" in folded or "\\package cache" in folded):
                    continue
                size = values.get("EstimatedSize")
                installed = str(values.get("InstallDate") or "")
                if len(installed) == 8 and installed.isdigit():
                    installed = f"{installed[:4]}-{installed[4:6]}-{installed[6:]}"
                apps[folded.rstrip("\\")] = {
                    "name": str(name), "publisher": values.get("Publisher"), "version": values.get("DisplayVersion"),
                    "installed": installed or None, "location": ntpath.normpath(location),
                    "registry_size": format_bytes(int(size) * 1024) if isinstance(size, int) else None,
                }
    return list(apps.values())


def version_info(path: str) -> dict:
    """CompanyName/ProductName/FileDescription/ProductVersion of an .exe or .dll."""
    try:
        version = ctypes.windll.version
    except (AttributeError, OSError):
        return {}
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return {}
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, buffer):
        return {}
    pointer, length = ctypes.c_void_p(), ctypes.c_uint()
    language, codepage = 0x0409, 0x04B0
    if version.VerQueryValueW(buffer, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)) and length.value >= 4:
        language, codepage = struct.unpack("<HH", ctypes.string_at(pointer.value, 4))
    found = {}
    for field in ("CompanyName", "ProductName", "FileDescription", "ProductVersion"):
        query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{field}"
        if version.VerQueryValueW(buffer, query, ctypes.byref(pointer), ctypes.byref(length)) and length.value:
            value = ctypes.wstring_at(pointer.value, length.value).rstrip("\0").strip()
            if value:
                found[field] = value
    return found


def download_source(path: str) -> dict:
    """Where a downloaded file came from, from its Zone.Identifier stream."""
    try:
        with open(path + ":Zone.Identifier", encoding="utf-8", errors="replace") as stream:
            text = stream.read(4000)
    except OSError:
        return {}
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    found = {key: values[key].strip(" \0") for key in ("HostUrl", "ReferrerUrl") if values.get(key, "").strip(" \0")}
    if values.get("ZoneId", "").strip() == "3":
        found["zone"] = "Internet"
    return found


class ItemProfiler:
    """Builds and caches one evidence profile per item in a completed scan."""

    def __init__(self, result: ScanResult, apps: Callable[[], list[dict]] = installed_apps) -> None:
        self.result = result
        self._apps_loader = apps
        self._apps: list[dict] | None = None
        self._cache: dict[int, dict] = {}

    def apps(self) -> list[dict]:
        if self._apps is None:
            try:
                self._apps = self._apps_loader()
            except OSError:
                self._apps = []
        return self._apps

    def _allocated(self, record: FileRecord) -> int:
        if record.is_directory:
            return self.result.directory_sizes.get(record.frn, (0, 0))[1]
        return record.allocated_size

    def owner(self, path: str) -> tuple[dict | None, list[str]]:
        """The installed app this path belongs to, and apps installed inside it."""
        folded = ntpath.normcase(path).rstrip("\\")
        owner, contained = None, []
        for app in self.apps():
            location = ntpath.normcase(app["location"]).rstrip("\\")
            if folded == location or folded.startswith(location + "\\"):
                if owner is None or len(location) > len(ntpath.normcase(owner["location"])):
                    owner = app
            elif location.startswith(folded + "\\"):
                contained.append(app["name"])
        return owner, sorted(contained)[:8]

    def label(self, entry: dict) -> str:
        """One-line description for choosing between items with the same name."""
        path = entry.get("path") or ""
        owner, _contained = self.owner(path)
        known = describe(path)
        role = (f"part of {owner['name']}" if owner else None) or (known["what"] if known and "inside" not in known else None)
        if role is None and known:
            role = f"inside {known['inside']} ({known['what'].rstrip('.')})"
        size = entry.get("allocated_display", "pending")
        return f"{size}, {role.rstrip('.')}" if role else f"{size}, {entry.get('kind', 'item')}"

    def _subtree(self, frn: int) -> dict:
        result = self.result
        now = time.time()
        by_type: dict[str, list[int]] = {}
        ages = {"changed in the last 30 days": 0, "30 days to 1 year": 0, "unchanged for over a year": 0}
        newest = oldest = 0
        files = folders = visited = 0
        pending = list(result.children.get(frn, ()))
        while pending and visited < _WALK_LIMIT:
            child = result.records.get(pending.pop())
            visited += 1
            if child is None:
                continue
            if child.is_directory:
                folders += 1
                if not child.attributes & 0x400:
                    pending.extend(result.children.get(child.frn, ()))
                continue
            files += 1
            size = child.allocated_size
            extension = ntpath.splitext(child.name)[1].casefold() or "(no extension)"
            group = by_type.setdefault(extension, [0, 0])
            group[0] += 1
            group[1] += size
            if child.modified:
                newest = max(newest, child.modified)
                oldest = min(oldest, child.modified) if oldest else child.modified
                age = now - child.modified
                bucket = "changed in the last 30 days" if age < 30 * _DAY else "30 days to 1 year" if age < 365 * _DAY else "unchanged for over a year"
                ages[bucket] += size
        total = sum(group[1] for group in by_type.values()) or 1
        types = [
            f"{extension}: {format_bytes(size)} in {count:,} file{'s' if count != 1 else ''} ({size * 100 // total}%)"
            for extension, (count, size) in heapq.nlargest(6, by_type.items(), key=lambda item: item[1][1])
        ]
        profile = {"files": files, "folders": folders, "largest_file_types": types,
                   "space_by_last_change": {bucket: format_bytes(size) for bucket, size in ages.items() if size},
                   "newest_change": _date(newest), "oldest_change": _date(oldest)}
        if pending:
            profile["partial"] = f"Stopped after {_WALK_LIMIT:,} items; figures cover part of the folder."
        return profile

    def profile(self, entry: dict) -> dict:
        frn = entry.get("frn")
        if frn is None or entry.get("kind") == "drive" or frn not in self.result.records:
            return {}
        if frn in self._cache:
            return self._cache[frn]
        record = self.result.records[frn]
        path = entry["path"]
        profile: dict = {"last_modified": _date(record.modified), "attributes": attribute_names(record.attributes)}
        try:
            stat = os.stat(path, follow_symlinks=False)
            profile["created"] = _date(getattr(stat, "st_birthtime", stat.st_ctime))
        except OSError:
            pass
        known = describe(path)
        if known:
            profile["known_location"] = known
        owner, contained = self.owner(path)
        if owner:
            profile["installed_app"] = {key: value for key, value in owner.items() if value}
        if contained:
            profile["apps_installed_inside"] = contained
        names = {}
        executables = []
        for child_frn in self.result.children.get(frn, ()):
            child = self.result.records.get(child_frn)
            if child is None:
                continue
            folded = child.name.casefold()
            marker = _MARKERS.get(folded) or _MARKER_SUFFIXES.get(ntpath.splitext(folded)[1])
            if marker:
                names.setdefault(marker, child.name)
            if not child.is_directory and folded.endswith(".exe") and not folded.startswith("unins"):
                executables.append(child)
        if names:
            profile["markers"] = [f"{marker} ({name})" for marker, name in names.items()]
        main = max(executables, key=lambda child: child.allocated_size) if executables else (
            record if record.name.casefold().endswith((".exe", ".dll")) else None)
        if main is not None:
            main_path = path if main is record else ntpath.join(path, main.name)
            details = version_info(main_path)
            if details:
                profile["program"] = {"file": main_path, **details}
        if record.is_directory:
            profile["contents"] = self._subtree(frn)
        else:
            source = download_source(path)
            if source:
                profile["downloaded_from"] = source
        self._cache[frn] = profile
        return profile
