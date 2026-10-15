import json
import time
import unittest

from sage.config import FILE_ATTRIBUTE_DIRECTORY as D
from sage.models import FileRecord, VolumeInfo
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from sage.services.known_folders import describe
from sage.services.local_ai import ConversationAssistant, OllamaClient
from sage.services.profile import ItemProfiler

MB = 1024 * 1024
OLD = int(time.time()) - 3 * 365 * 86400
RECENT = int(time.time()) - 86400
STEAM_APP = {"name": "Steam", "publisher": "Valve Corporation", "version": "2.10.91.91",
             "installed": "2024-03-15", "location": "C:\\Program Files (x86)\\Steam"}


def steam_scan():
    folders = [
        (10, 5, "Program Files (x86)"), (11, 10, "Steam"), (12, 11, "steamapps"), (13, 12, "common"),
        (14, 13, "Portal 2"), (20, 5, "Users"), (21, 20, "Ada"), (22, 21, "AppData"), (23, 22, "Local"),
        (24, 23, "Steam"), (25, 24, "htmlcache"), (26, 21, "Downloads"),
    ]
    files = [
        (100, 11, "steam.exe", 4, RECENT), (101, 14, "pak01_000.vpk", 900, OLD), (102, 14, "pak01_001.vpk", 600, OLD),
        (103, 14, "portal2.exe", 20, OLD), (104, 25, "data_1", 30, RECENT), (105, 26, "setup.zip", 50, RECENT),
    ]
    records = {5: FileRecord(5, 5, ".", D)}
    records.update({frn: FileRecord(frn, parent, name, D) for frn, parent, name in folders})
    records.update({frn: FileRecord(frn, parent, name, 0, size * MB, size * MB, size_ready=True, modified=modified)
                    for frn, parent, name, size, modified in files})
    result = build_scan_result(VolumeInfo("C:", "Windows", "NTFS"), records)
    calculate_directory_sizes(result)
    return result


def assistant_for(result, payloads):
    client = OllamaClient(transport=lambda payload: payloads.append(payload) or {"message": {"content": "Reviewed."}})
    assistant = ConversationAssistant(result, client)
    assistant.profiler = ItemProfiler(result, apps=lambda: [STEAM_APP])
    return assistant


class KnownFolderTests(unittest.TestCase):
    def test_specific_entry_wins_and_unknown_child_reports_its_known_parent(self):
        self.assertIn("One installed Steam game", describe("D:\\SteamLibrary\\steamapps\\common\\Portal 2")["what"])
        nested = describe("C:\\Windows\\WinSxS\\amd64_example")
        self.assertIn("component store", nested["what"])
        self.assertEqual(nested["inside"], "C:\\Windows\\WinSxS")
        self.assertIsNone(describe("E:\\Projects\\notes.txt"))


class ProfileTests(unittest.TestCase):
    def test_profile_names_owner_markers_contents_and_age(self):
        result = steam_scan()
        profiler = ItemProfiler(result, apps=lambda: [STEAM_APP])
        profile = profiler.profile({"frn": 11, "path": "C:\\Program Files (x86)\\Steam", "kind": "folder"})
        self.assertEqual(profile["installed_app"]["publisher"], "Valve Corporation")
        self.assertIn("Steam client (steam.exe)", profile["markers"])
        self.assertTrue(profile["contents"]["largest_file_types"][0].startswith(".vpk: 1.5 GB in 2 files"))
        self.assertEqual(profile["contents"]["space_by_last_change"]["unchanged for over a year"], "1.5 GB")
        self.assertIn("Steam client by Valve", profile["known_location"]["what"])


class ProfiledAnswerTests(unittest.TestCase):
    def test_same_named_folders_are_listed_with_size_and_role(self):
        payloads = []
        reply = assistant_for(steam_scan(), payloads).answer("Should I delete steam?")
        self.assertIn("`C:\\Program Files (x86)\\Steam` — 1.5 GB, part of Steam", reply)
        self.assertIn("`C:\\Users\\Ada\\AppData\\Local\\Steam` — 30.0 MB, Steam's built-in web browser cache", reply)
        self.assertEqual(payloads, [])

    def test_protected_install_refusal_is_built_from_the_profile(self):
        payloads = []
        reply = assistant_for(steam_scan(), payloads).answer("Should I delete C:\\Program Files (x86)\\Steam?")
        self.assertIn("Do not directly delete", reply)
        self.assertIn("**Steam** (Valve Corporation, 2.10.91.91), installed 2024-03-15", reply)
        self.assertIn(".vpk: 1.5 GB", reply)
        self.assertIn("1.5 GB of it has not changed for over a year", reply)
        self.assertIn("Installed apps", reply)
        self.assertEqual(payloads, [])

    def test_model_receives_profile_for_the_chosen_item(self):
        payloads = []
        assistant = assistant_for(steam_scan(), payloads)
        assistant.answer("Should I delete steam?")
        self.assertIn("Steam's built-in web browser cache", assistant.answer("2"))
        self.assertEqual(payloads, [])
        reply = assistant.answer("What is it?")
        self.assertEqual(payloads, [])
        self.assertIn("C:\\Users\\Ada\\AppData\\Local\\Steam", reply)
        self.assertIn("web browser cache", reply)
        self.assertIn("changed in the last 30 days", reply)


if __name__ == "__main__":
    unittest.main()
