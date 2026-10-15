import json
import os
import tempfile
import unittest

from sage.config import FILE_ATTRIBUTE_DIRECTORY
from sage.models import FileRecord, VolumeInfo
from sage.platform.windows.file_operations import delete_permanently
from sage.services.cleanup import find_cleanup_suggestions
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from sage.services.local_ai import ConversationAssistant, OllamaClient

MB = 1024 * 1024
D = FILE_ATTRIBUTE_DIRECTORY


def cleanup_scan():
    folders = [
        (10, 5, "Users"), (11, 10, "hp"), (12, 11, "AppData"), (13, 12, "Local"),
        (14, 13, "Temp"), (15, 13, "Google"), (16, 15, "Chrome"), (17, 16, "User Data"),
        (18, 17, "Default"), (19, 18, "Cache"), (20, 5, "Windows"), (21, 20, "SoftwareDistribution"),
        (22, 21, "Download"), (23, 11, "Projects"), (24, 23, "site"), (25, 24, "node_modules"),
        (26, 25, "node_modules"), (27, 11, "Documents"), (28, 11, "Downloads"),
        (29, 5, "Program Files"), (30, 5, "Program Files (x86)"), (31, 30, "Steam"), (32, 30, "Zoom"),
        (33, 5, "Tools"), (34, 33, "resources"), (35, 34, "app"), (36, 35, "node_modules"),
        (37, 23, "loose"), (38, 37, "node_modules"),
    ]
    records = {5: FileRecord(5, 5, ".", D)}
    records.update({frn: FileRecord(frn, parent, name, D) for frn, parent, name in folders})
    files = [
        (100, 14, "setup.log", 300), (101, 19, "data_1", 900), (102, 22, "kb.cab", 1200),
        (103, 26, "lib.js", 400), (104, 27, "thesis.docx", 5000), (105, 28, "tool-setup.exe", 250),
        (106, 28, "photo.png", 800), (107, 14, "tiny.tmp", 1),
        (108, 31, "game.pak", 9000), (109, 32, "zoom.exe", 300), (110, 29, "app.exe", 50),
        (111, 24, "package.json", 0), (112, 36, "bundled.js", 500), (113, 35, "package.json", 0),
        (114, 38, "orphan.js", 600),
    ]
    records.update({
        frn: FileRecord(frn, parent, name, 0, size * MB, size * MB, size_ready=True)
        for frn, parent, name, size in files
    })
    result = build_scan_result(VolumeInfo("C:", "Windows", "NTFS"), records)
    calculate_directory_sizes(result)
    return result


class CleanupSuggestionTests(unittest.TestCase):
    def test_lists_specific_regenerable_items_largest_first(self):
        suggestions = find_cleanup_suggestions(cleanup_scan())
        paths = [s.path for s in suggestions]
        self.assertEqual(paths, [
            "C:\\Windows\\SoftwareDistribution\\Download",
            "C:\\Users\\hp\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\Cache",
            "C:\\Users\\hp\\Projects\\site\\node_modules",
            "C:\\Users\\hp\\AppData\\Local\\Temp",
            "C:\\Users\\hp\\Downloads\\tool-setup.exe",
        ])
        self.assertTrue(suggestions[0].contents_only)
        self.assertFalse(suggestions[2].contents_only)
        self.assertNotIn("thesis.docx", " ".join(paths))
        self.assertNotIn("photo.png", " ".join(paths))
        self.assertNotIn("C:\\Tools\\resources\\app\\node_modules", paths)
        self.assertNotIn("C:\\Users\\hp\\Projects\\loose\\node_modules", paths)

    def test_cleanup_question_answers_without_model_and_returns_buttons(self):
        def forbidden(_payload):
            self.fail("Cleanup lists come from rules, not the model")

        assistant = ConversationAssistant(cleanup_scan(), OllamaClient(transport=forbidden))
        answer = assistant.answer("I want to clear some space in c drive, which files are safe to delete")
        self.assertIn("You can free about", answer)
        self.assertEqual(len(assistant.last_suggestions), 5)

    def test_verified_cache_under_windows_is_not_refused(self):
        def fake(_payload):
            return {"message": {"content": "C:\\Windows\\SoftwareDistribution\\Download uses 1.2 GB; delete it below."}}

        assistant = ConversationAssistant(cleanup_scan(), OllamaClient(transport=fake))
        answer = assistant.answer("Can I delete C:\\Windows\\SoftwareDistribution\\Download?")
        self.assertNotIn("Do not directly delete", answer)
        self.assertEqual([s.path for s in assistant.last_suggestions], ["C:\\Windows\\SoftwareDistribution\\Download"])


class ScopedQuestionTests(unittest.TestCase):
    def _assistant(self):
        payloads = []

        def fake(payload):
            payloads.append(payload)
            return {"message": {"content": "Steam uses 8.8 GB; uninstall it from Settings if unused."}}

        return ConversationAssistant(cleanup_scan(), OllamaClient(transport=fake)), payloads

    @staticmethod
    def _context(payload):
        return json.loads(payload["messages"][-1]["content"].split("(JSON): ")[1])

    def test_named_folder_question_is_scoped_not_the_drive_wide_list(self):
        assistant, payloads = self._assistant()
        answer = assistant.answer("what can i delete in program files (x86)?")
        self.assertEqual(payloads, [])
        self.assertNotIn("You can free about", answer)
        self.assertIn("C:\\Program Files (x86)", answer)
        self.assertIn("Decision: do not delete", answer)
        self.assertIn("Steam", answer)
        self.assertNotIn("Name a child folder", answer)

    def test_different_folders_get_different_evidence(self):
        assistant, payloads = self._assistant()
        downloads = assistant.answer("what is inside Downloads?")
        documents = assistant.answer("what is inside Documents?")
        self.assertEqual(payloads, [])
        self.assertIn("C:\\Users\\hp\\Downloads", downloads)
        self.assertNotIn("Documents", downloads)
        self.assertIn("C:\\Users\\hp\\Documents", documents)
        self.assertNotIn("Downloads", documents)

    def test_selected_text_is_resolved_and_sent(self):
        assistant, payloads = self._assistant()
        quote = "1. 1.2 GB  C:\\Windows\\SoftwareDistribution\\Download\nWindows Update download cache."
        assistant.answer("is this safe to delete?", quoted=quote)
        context = self._context(payloads[0])
        self.assertEqual(context["selected_text"], quote)
        self.assertEqual(context["matched_items"][0]["path"], "C:\\Windows\\SoftwareDistribution\\Download")
        self.assertIn("Windows Update download cache", context["matched_items"][0]["verified_cleanup"])
        self.assertEqual([s.path for s in assistant.last_suggestions], ["C:\\Windows\\SoftwareDistribution\\Download"])

    def test_quoted_suggestion_ignores_protected_tree_selection(self):
        assistant, payloads = self._assistant()
        quote = "2. 250.0 MB C:\\Users\\hp\\Downloads\\tool-setup.exe Installer in Downloads."
        answer = assistant.answer("Is this really safe to delete?", ("C:\\Program Files",), quoted=quote)
        self.assertNotIn("Do not directly delete", answer)
        context = self._context(payloads[0])
        self.assertEqual(context["selected_items"], [])
        self.assertIn("Installer in Downloads", context["matched_items"][0]["verified_cleanup"])

    def test_every_suggestion_reaches_the_model_and_escaped_paths_are_fixed(self):
        payloads = []

        def fake(payload):
            payloads.append(payload)
            return {"message": {"content": "C:\\\\Users\\\\hp\\\\Downloads\\\\tool-setup.exe is safe to delete."}}

        assistant = ConversationAssistant(cleanup_scan(), OllamaClient(transport=fake))
        answer = assistant.answer("is C:\\Users\\hp\\Downloads\\tool-setup.exe safe to delete?")
        candidates = self._context(payloads[0])["cleanup_candidates"]
        self.assertEqual(len(candidates), len(assistant.cleanup_suggestions()))
        self.assertTrue(answer.startswith("C:\\Users\\hp\\Downloads\\tool-setup.exe"))


class PermanentDeleteTests(unittest.TestCase):
    def test_contents_only_keeps_folder_and_full_delete_removes_it(self):
        with tempfile.TemporaryDirectory() as root:
            cache = os.path.join(root, "Cache")
            os.makedirs(os.path.join(cache, "nested"))
            for name in ("a.bin", os.path.join("nested", "b.bin")):
                with open(os.path.join(cache, name), "wb") as stream:
                    stream.write(b"x")
            os.chmod(os.path.join(cache, "a.bin"), 0o444)
            self.assertEqual(delete_permanently(cache, contents_only=True), 0)
            self.assertEqual(os.listdir(cache), [])
            self.assertEqual(delete_permanently(cache), 0)
            self.assertFalse(os.path.exists(cache))

    def test_refuses_drive_root(self):
        with self.assertRaises(ValueError):
            delete_permanently(os.path.abspath(os.sep))


if __name__ == "__main__":
    unittest.main()
