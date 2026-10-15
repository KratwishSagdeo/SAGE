"""What well-known Windows locations are, and the supported way to reclaim their space.

Patterns are matched against the drive-relative, lowercase path. The first match
wins, so specific entries come before generic ones.
"""

from __future__ import annotations

import ntpath
import re

_U = r"users\\[^\\]+\\"
_PF = r"program files(?: \(x86\))?\\"

_ENTRIES = (
    # Games
    (r"(?:.*\\)?steamapps\\common\\[^\\]+", "One installed Steam game.",
     "Uninstall it from Steam (Library > right-click > Manage > Uninstall) so Steam's library stays consistent."),
    (r"(?:.*\\)?steamapps\\common", "Installed Steam games, one folder per game.",
     "Uninstall games you no longer play from inside Steam; deleting folders here leaves Steam thinking they are installed."),
    (r"(?:.*\\)?steamapps\\shadercache", "Steam's compiled shader cache.",
     "Safe to clear with Steam closed; games rebuild shaders and may stutter briefly on first launch."),
    (r"(?:.*\\)?steamapps\\downloading", "Partially downloaded Steam games and updates.",
     "Safe to clear when Steam is not downloading; Steam downloads them again."),
    (r"(?:.*\\)?steamapps\\workshop", "Steam Workshop mods and community content for your games.",
     "Unsubscribe from items in Steam Workshop instead of deleting them here."),
    (r"(?:.*\\)?steamapps", "Steam library data: installed games, downloads, shader cache and workshop content.",
     "Manage games from inside Steam; shadercache and downloading are the parts that are safe to clear."),
    (_PF + r"steam", "Steam client by Valve. By default it also holds your installed games under steamapps.",
     "Uninstall Steam via Settings > Apps > Installed apps; that also removes games stored here. To keep Steam, uninstall individual games in Steam."),
    (r"(?:.*\\)?steamlibrary", "A Steam game library folder on this drive.",
     "Manage or move it from Steam > Settings > Storage."),
    (_U + r"appdata\\local\\steam(?:\\htmlcache)?", "Steam's built-in web browser cache.",
     "Can be cleared with Steam closed; Steam rebuilds it."),
    (_PF + r"epic games\\launcher\\vaultcache", "Epic Games launcher's Unreal Engine marketplace download cache.",
     "Clear it from the Epic Games launcher (Unreal Engine > Library > Vault > Set cache location / clear)."),
    (_PF + r"epic games", "Epic Games launcher and the games installed through it.",
     "Uninstall games from the Epic Games launcher; uninstall the launcher via Installed apps."),
    # Browsers and chat apps
    (_U + r"appdata\\local\\google\\chrome\\user data", "Google Chrome profiles: history, passwords, extensions, cookies and cache.",
     "Do not delete it; clear browsing data in Chrome settings, which keeps your profile."),
    (_U + r"appdata\\local\\microsoft\\edge\\user data", "Microsoft Edge profiles: history, passwords, extensions and cache.",
     "Do not delete it; clear browsing data in Edge settings."),
    (_U + r"appdata\\local\\bravesoftware\\brave-browser\\user data", "Brave browser profiles and cache.",
     "Clear browsing data in Brave settings instead of deleting the folder."),
    (_U + r"appdata\\roaming\\mozilla\\firefox\\profiles", "Firefox profiles: bookmarks, passwords, history and add-ons.",
     "Do not delete; clear history and cache in Firefox settings."),
    (_U + r"appdata\\(?:local|roaming)\\discord", "Discord app files and cache.",
     "Cache subfolders can be cleared with Discord closed; uninstall Discord via Installed apps."),
    (_U + r"appdata\\local\\packages\\mstea[^\\]*", "New Microsoft Teams app data and cache.",
     "Reset Teams from Settings > Apps > Installed apps > Teams > Advanced options."),
    # Developer tools
    (_U + r"\.(?:vscode|cursor)\\extensions", "Installed editor extensions (VS Code or Cursor).",
     "Uninstall extensions from inside the editor; old versions are removed automatically."),
    (_U + r"appdata\\roaming\\(?:code|cursor)", "Editor settings, workspace state and caches (VS Code or Cursor).",
     "Keep it for your settings; its Cache, CachedData and logs subfolders can be cleared with the editor closed."),
    (_U + r"appdata\\local\\android\\sdk", "Android SDK: platforms, build tools, emulator and system images.",
     "Remove unused platforms and system images in Android Studio > SDK Manager."),
    (_U + r"\.android\\avd", "Android emulator virtual devices.",
     "Delete unused devices in Android Studio > Device Manager."),
    (_U + r"\.gradle", "Gradle home: downloaded dependencies, build caches and wrapper distributions.",
     "caches and wrapper\\dists are re-downloaded when needed; delete them with no build running."),
    (_U + r"\.m2\\repository", "Maven local repository of downloaded dependencies.",
     "Re-downloaded on the next build; delete old artifacts or the whole repository when no build is running."),
    (_U + r"\.cache\\huggingface", "Hugging Face models and datasets downloaded for AI tools.",
     "Delete models you no longer use (huggingface-cli delete-cache); they can be downloaded again."),
    (_U + r"(?:anaconda3|miniconda3)\\pkgs", "Conda's package download cache.",
     "Run conda clean --all to remove cached packages safely."),
    (_U + r"(?:anaconda3|miniconda3)", "Anaconda/Miniconda Python installation and its environments.",
     "Remove unused environments with conda env remove -n NAME; uninstall Conda via Installed apps."),
    (_U + r"appdata\\local\\docker", "Docker Desktop data, including the WSL disk images that hold your images and containers.",
     "Run docker system prune (or Docker Desktop > Troubleshoot > Clean up); never delete the .vhdx files directly."),
    (_U + r"appdata\\local\\packages\\canonicalgroup[^\\]*", "A WSL Linux distribution (Ubuntu); its ext4.vhdx is the Linux disk.",
     "Free space inside Linux, or remove the distro with wsl --unregister NAME (this deletes it)."),
    (r"(?:.*\\)?node_modules", "JavaScript dependencies installed for a project.",
     "If it sits next to package.json, npm install restores it; inside an installed app it is part of the app."),
    (r"(?:.*\\)?\.git", "Git repository history and branches.",
     "Deleting it loses local history and unpushed work; keep it with its project."),
    (r"(?:.*\\)?\.?venv", "A Python virtual environment.",
     "Recreate it from requirements.txt or pyproject.toml if deleted; check the project still needs it."),
    (r"(?:.*\\)?__pycache__", "Python bytecode cache.", "Safe to delete; Python regenerates it."),
    # Windows
    (r"windows\\winsxs", "Windows component store, used for updates, repair and optional features.",
     "Never delete files here; shrink it with DISM /Online /Cleanup-Image /StartComponentCleanup."),
    (r"windows\\installer", "Windows Installer cache needed to repair, update and uninstall apps.",
     "Do not delete; removing files breaks uninstall and updates for those apps."),
    (r"windows\\system32\\driverstore", "Windows driver store.", "Do not delete; remove old drivers through Device Manager."),
    (r"windows\\softwaredistribution", "Windows Update working folder.",
     "Its Download subfolder can be cleared; use Settings > System > Storage > Temporary files for the rest."),
    (r"windows\\temp", "System temporary files.", "Clear via Settings > System > Storage > Temporary files."),
    (r"windows", "Windows operating system files.", "Never delete; use Settings > System > Storage > Cleanup recommendations."),
    (r"windows\.old", "Your previous Windows installation, kept after an upgrade.",
     "Remove it via Settings > System > Storage > Temporary files > Previous Windows installation(s); you can no longer roll back."),
    (r"\$recycle\.bin", "The Recycle Bin.", "Empty the Recycle Bin to free this space."),
    (r"system volume information", "System Restore points and volume shadow copies.",
     "Manage restore points in System Protection settings; do not delete this folder."),
    (r"hiberfil\.sys", "Hibernation file, also used by Fast Startup.",
     "powercfg /h off removes it, which disables hibernation and Fast Startup."),
    (r"pagefile\.sys|swapfile\.sys", "Windows virtual memory file.",
     "Do not delete; change its size in System > Advanced system settings > Performance > Virtual memory."),
    (r"program files\\windowsapps", "Installed Microsoft Store apps.", "Uninstall Store apps via Settings > Apps > Installed apps."),
    (r"programdata\\usoshared", "Windows Update Orchestrator shared logs and state.",
     "Do not delete or move it. Windows Update keeps this path."),
    (r"programdata\\uso", "Windows Update Orchestrator working data.",
     "Do not delete or move it. Windows Update keeps this path."),
    (r"programdata\\mozilla", "Firefox and other Mozilla apps' machine-wide data.",
     "Do not move it. Uninstall Firefox from Installed apps if you no longer want this data."),
    (r"programdata\\microsoft", "Microsoft and Windows shared application data.",
     "Do not delete or move it. Inspect a specific subfolder instead."),
    (r"programdata\\package cache", "Installer cache for Visual Studio, .NET and other bundles.",
     "Needed to repair and uninstall those programs; do not delete it."),
    (r"programdata", "Machine-wide data used by Windows and installed programs.",
     "Do not move this folder. Ask about a named folder inside it."),
    (r"programdata\\nvidia corporation\\downloader", "NVIDIA driver packages downloaded by GeForce Experience/NVIDIA app.",
     "Safe to delete after the driver is installed."),
    (r"nvidia|amd", "Extracted graphics driver installer files.", "Safe to delete once the driver is installed."),
    (_PF + r"microsoft office", "Microsoft Office.", "Uninstall or modify Office via Settings > Apps > Installed apps."),
    (r"documents and settings", "A compatibility link to your user profiles, not your Documents folder.",
     "Do not delete or move it. It holds no files. Your documents are under Users."),
    # Personal folders
    (_U + r"onedrive(?: - [^\\]+)?", "Your OneDrive folder, synced with the cloud.",
     "Deleting here also deletes from OneDrive; use 'Free up space' to keep files online-only instead."),
    (_U + r"downloads", "Your Downloads folder.", "Review old installers, archives and videos; nothing here is needed by Windows."),
    (_U + r"(?:documents|pictures|videos|music|desktop)", "One of your personal folders.",
     "Personal files: only you can decide what to keep; back up before deleting."),
    (_U + r"appdata\\local\\temp", "Your temporary files.", "Clear via Settings > System > Storage > Temporary files."),
    (_U + r"appdata\\local\\packages\\[^\\]+", "Data for a Microsoft Store app.", "Reset or uninstall the app via Installed apps."),
    (_U + r"appdata\\local\\programs\\[^\\]+", "An app installed for your user account only.", "Uninstall it via Settings > Apps > Installed apps."),
    (_U + r"appdata\\(?:local|roaming|locallow)\\[^\\]+", "Settings, data and caches of one application.",
     "Do not delete the whole folder; clear the app's cache from its own settings, or uninstall the app."),
    (_U + r"appdata", "Application settings and data for your user account.", "Do not delete; inspect individual app folders instead."),
    (r"users\\[^\\]+", "A user profile.", "Do not delete a profile folder; remove accounts via Settings > Accounts."),
    (r"users", "Container of all user profiles.", "Do not delete; inspect the folders inside a profile instead."),
    # Generic install locations last
    (_PF + r"[^\\]+", "An installed application.", "Uninstall it via Settings > Apps > Installed apps."),
    (r"program files(?: \(x86\))?", "Installed applications.", "Uninstall unused apps via Settings > Apps > Installed apps."),
    (r"programdata\\[^\\]+", "Machine-wide data and settings of one application.", "Uninstall the owning app instead of deleting this."),
)
_COMPILED = tuple((re.compile(pattern), what, advice) for pattern, what, advice in _ENTRIES)


def describe(path: str) -> dict | None:
    """Knowledge for the path itself or, failing that, its nearest known ancestor."""
    drive, tail = ntpath.splitdrive(path)
    parts = [part for part in tail.casefold().split("\\") if part]
    for depth in range(len(parts), 0, -1):
        relative = "\\".join(parts[:depth])
        for pattern, what, advice in _COMPILED:
            if pattern.fullmatch(relative):
                known = {"what": what, "advice": advice}
                if depth < len(parts):
                    known["inside"] = drive + "\\" + "\\".join(path.split("\\")[1:depth + 1]).lstrip("\\")
                return known
    return None
