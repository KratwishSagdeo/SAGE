"""Find specific, regenerable space hogs in a finished scan.

Only paths matching these rules are offered for deletion. Everything else stays
a review item, so a model reply can never turn an arbitrary path into a delete
button.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import ScanResult
from .evidence import format_bytes

MIN_SUGGESTION_BYTES = 20 * 1024 * 1024
MAX_SUGGESTIONS = 12
_USER = r"users\\[^\\]+\\"


@dataclass(frozen=True, slots=True)
class CleanupSuggestion:
    path: str
    title: str
    reason: str
    allocated: int
    contents_only: bool = True
    action: str = "delete"


@dataclass(frozen=True, slots=True)
class _Rule:
    names: frozenset[str]
    pattern: re.Pattern[str]
    title: str
    reason: str
    contents_only: bool = True
    action: str = "delete"
    directory: bool = True
    suffixes: tuple[str, ...] = ()


def _rule(names: str, pattern: str, title: str, reason: str, **options) -> _Rule:
    return _Rule(frozenset(names.split("|")), re.compile(pattern), title, reason, **options)


# Paths are matched relative to the drive root, lowercase, without a leading backslash.
_RULES = (
    _rule("$recycle.bin", r"^\$recycle\.bin$", "Recycle Bin",
          "Files you already deleted. Emptying the bin frees this space.", action="empty_recycle_bin"),
    _rule("windows.old|$windows.~bt|$windows.~ws", r"^(windows\.old|\$windows\.~bt|\$windows\.~ws)$",
          "Previous Windows installation / upgrade files",
          "Left over from a Windows upgrade. After deleting, you can no longer roll back that upgrade.",
          contents_only=False),
    _rule("download", r"^windows\\softwaredistribution\\download$", "Windows Update download cache",
          "Update packages that are already installed. Windows downloads again anything it still needs."),
    _rule("deliveryoptimization", r"^windows\\softwaredistribution\\deliveryoptimization$",
          "Delivery Optimization cache", "Update pieces cached for sharing with other PCs. Safe to clear."),
    _rule("cache", r"^windows\\serviceprofiles\\networkservice\\appdata\\local\\microsoft\\windows\\deliveryoptimization\\cache$",
          "Delivery Optimization cache", "Update pieces cached for sharing with other PCs. Safe to clear."),
    _rule("temp", r"^windows\\temp$", "Windows temporary files",
          "Leftovers from installers and system tasks. Files still in use are skipped."),
    _rule("temp", _USER + r"appdata\\local\\temp$", "Your temporary files",
          "Leftovers from apps and installers. Close apps first; files still in use are skipped."),
    _rule("minidump|livekernelreports", r"^windows\\(minidump|livekernelreports)$", "Crash dump files",
          "Diagnostic dumps from past crashes. Only useful if you are debugging a crash."),
    _rule("reportarchive|reportqueue", r"^programdata\\microsoft\\windows\\wer\\(reportarchive|reportqueue)$",
          "Windows error reports", "Archived crash reports. Windows does not need them to run."),
    _rule("crashdumps", _USER + r"appdata\\local\\crashdumps$", "App crash dumps",
          "Dumps written when apps crashed. Only useful for debugging."),
    _rule("memory.dmp", r"^windows\\memory\.dmp$", "System crash dump",
          "Full memory dump from a past blue screen. Only useful for debugging.", directory=False),
    _rule("cache|code cache|gpucache|cachestorage",
          _USER + r"appdata\\local\\.+\\user data\\[^\\]+\\(cache|code cache|gpucache|service worker\\cachestorage)$",
          "Browser cache", "Cached web pages and scripts. The browser rebuilds it; close the browser first."),
    _rule("cache2", _USER + r"appdata\\local\\mozilla\\firefox\\profiles\\[^\\]+\\cache2$", "Firefox cache",
          "Cached web pages. Firefox rebuilds it; close Firefox first."),
    _rule("cache|code cache|gpucache|cacheddata|cachestorage",
          _USER + r"appdata\\roaming\\[^\\]+\\(cache|code cache|gpucache|cacheddata|service worker\\cachestorage)$",
          "App cache (Discord, Teams, VS Code, Slack, …)", "Cache the app rebuilds on its own. Close the app first."),
    _rule("inetcache", _USER + r"appdata\\local\\microsoft\\windows\\inetcache$", "Internet cache",
          "Cached web content used by Windows components. Rebuilt automatically."),
    _rule("dxcache|glcache|dxccache|d3dscache",
          _USER + r"appdata\\local\\(nvidia\\(dxcache|glcache)|amd\\(dxcache|glcache|dxccache)|d3dscache)$",
          "GPU shader cache", "Compiled shaders. Games rebuild them; the first launch afterward may stutter."),
    _rule("cache", _USER + r"appdata\\local\\pip\\cache$", "pip download cache",
          "Python packages already installed. pip downloads them again only if needed."),
    _rule("npm-cache", _USER + r"appdata\\(local|roaming)\\npm-cache$", "npm cache",
          "Downloaded npm packages. npm downloads them again only if needed."),
    _rule("cache", _USER + r"appdata\\local\\yarn\\cache$", "Yarn cache",
          "Downloaded Yarn packages. Yarn downloads them again only if needed."),
    _rule("v3-cache", _USER + r"appdata\\local\\nuget\\v3-cache$", "NuGet HTTP cache",
          "Downloaded NuGet metadata and packages. Rebuilt on the next restore."),
    _rule("caches", _USER + r"\.gradle\\caches$", "Gradle cache",
          "Downloaded build dependencies. Gradle downloads them again on the next build."),
    _rule("go-build", _USER + r"appdata\\local\\go-build$", "Go build cache",
          "Compiled Go build output. Rebuilt on the next build."),
    _rule("node_modules", r"(^|\\)node_modules$", "node_modules (project dependencies)",
          "Installed packages for a JavaScript project. Run npm install in that project to restore them.",
          contents_only=False),
    _rule("", _USER + r"downloads\\[^\\]+$", "Installer in Downloads",
          "A setup or disk image file. You can download it again if you need to reinstall.",
          contents_only=False, directory=False, suffixes=(".exe", ".msi", ".msix", ".iso")),
)


def find_cleanup_suggestions(
    result: ScanResult, limit: int = MAX_SUGGESTIONS, minimum: int = MIN_SUGGESTION_BYTES
) -> list[CleanupSuggestion]:
    """Return the largest rule-matched items, without nesting one inside another."""
    if not result.sizes_complete:
        return []
    prefix = len(result.volume.drive) + 1
    found: list[CleanupSuggestion] = []
    for record in result.records.values():
        if record.frn == result.root_frn:
            continue
        name = record.name.casefold()
        relative: str | None = None
        for rule in _RULES:
            if rule.directory != record.is_directory:
                continue
            if name not in rule.names and not (rule.suffixes and name.endswith(rule.suffixes)):
                continue
            allocated = (
                result.directory_sizes.get(record.frn, (0, 0))[1]
                if record.is_directory else record.allocated_size
            )
            if allocated < minimum:
                break
            if relative is None:
                path = result.path_for(record.frn)
                if path is None:
                    break
                relative = path[prefix:].casefold()
            if rule.pattern.search(relative):
                found.append(CleanupSuggestion(
                    path, rule.title, rule.reason, allocated, rule.contents_only, rule.action
                ))
                break
    found.sort(key=lambda suggestion: suggestion.allocated, reverse=True)
    chosen: list[CleanupSuggestion] = []
    for suggestion in found:
        key = suggestion.path.casefold() + "\\"
        if any(key.startswith(other) or other.startswith(key) for other in (c.path.casefold() + "\\" for c in chosen)):
            continue
        chosen.append(suggestion)
        if len(chosen) >= limit:
            break
    return chosen


def cleanup_report(result: ScanResult, suggestions: list[CleanupSuggestion]) -> str:
    drive = result.volume.drive
    if not result.sizes_complete:
        return "Sizes are still being measured. I'll list exactly what you can delete once they finish."
    if not suggestions:
        return (
            f"I found no caches, crash dumps, update leftovers, or old installers over "
            f"{format_bytes(MIN_SUGGESTION_BYTES)} on {drive}. The remaining space is in installed "
            "apps and personal files. Ask about the largest folders to review them."
        )
    total = sum(suggestion.allocated for suggestion in suggestions)
    return (
        f"**You can free about {format_bytes(total)} on {drive}** with these {len(suggestions)} "
        "items, largest first. Each one is a cache or leftover that Windows or the app recreates, "
        "or a file you can download again. Deletion is **permanent** (it skips the Recycle Bin), "
        "and files that are in use are skipped."
    )
