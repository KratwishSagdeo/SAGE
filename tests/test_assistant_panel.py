import tkinter as tk
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from sage.ui.assistant_panel import AssistantPanel
from sage.ui.markdown import markdown_spans
from sage.ui.application import SageApplication
from sage.ui.tree_panel import TreePanel
from sage.platform.windows.file_operations import validate_mutation, shell_file_operation
from unittest.mock import patch


class MarkdownTests(unittest.TestCase):
    def test_bold_code_bullets_and_windows_paths(self):
        spans = markdown_spans("**Total Size:** **354 MB**\n* Folder: `F:\\Git`\n")
        text = "".join(value for value, _tags in spans)
        self.assertEqual(text, "Total Size: 354 MB\n• Folder: F:\\Git\n")
        self.assertIn(("354 MB", ("bold",)), spans)
        self.assertIn(("F:\\Git", ("code",)), spans)

    def test_partial_bold_does_not_expose_delimiters(self):
        self.assertEqual(markdown_spans("**Total", partial=True), [("Total", ("bold",))])
        self.assertEqual(markdown_spans("**Total"), [("Total", ("bold",))])


class AssistantPanelTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.panel = AssistantPanel(self.root, lambda _question: None)

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def test_stream_formatting_preserves_other_scan_messages(self):
        self.panel.begin_reply()
        self.assertEqual(str(self.panel.ask_button["state"]), "disabled")
        self.panel.set_status("Writing the answer…")
        self.panel.append_reply("**Total")
        self.panel.after_cancel(self.panel._render_job)
        self.panel._render_reply()
        self.panel.write("SAGE", "Size analysis complete.")
        self.panel.append_reply(" Size:** **354 MB** in `F:\\Git`.")
        self.panel.finish_reply("**Total Size:** **354 MB** in `F:\\Git`.")
        text = self.panel.transcript.get("1.0", "end-1c")
        self.assertIn("Total Size: 354 MB in F:\\Git.", text)
        self.assertIn("Size analysis complete.", text)
        self.assertNotIn("**", text)
        self.assertTrue(self.panel.transcript.tag_ranges("bold"))
        self.assertEqual(str(self.panel.ask_button["state"]), "normal")
        self.assertFalse(self.panel._busy)

    def test_navigation_has_no_selection_but_explicit_menu_can_choose_a_target(self):
        tree = TreePanel(self.root).tree
        tree.insert("", "end", iid="frn:10", text="Folder")
        self.assertEqual(str(tree["selectmode"]), "none")
        tree.focus("frn:10")
        self.assertEqual(tree.selection(), ())
        tree.selection_set("frn:10")
        self.assertEqual(tree.selection(), ("frn:10",))


class AssistantEventTests(unittest.TestCase):
    def test_old_answer_cannot_replace_current_request(self):
        events = queue.SimpleQueue()
        events.put(("assistant_token", 3, 10, "old text"))
        events.put(("assistant_answer", 3, 10, "old answer"))
        events.put(("assistant_token", 3, 11, "current text"))
        events.put(("assistant_answer", 3, 11, "current answer"))
        panel = Mock()
        app = SimpleNamespace(_events=events, _generation=3, _assistant_request_id=11, _assistant_busy=True, assistant_panel=panel, winfo_exists=lambda: False)
        SageApplication._drain_events(app)
        panel.append_reply.assert_called_once_with("current text")
        panel.finish_reply.assert_called_once_with("current answer")
        self.assertFalse(app._assistant_busy)

    def test_question_worker_never_reads_tree_selection(self):
        conversation = Mock()
        conversation.answer.return_value = "answer"
        app = SimpleNamespace(_conversation=conversation, _assistant_busy=False,
                              _assistant_request_id=0, _generation=1,
                              assistant_panel=Mock(), _events=queue.SimpleQueue(),
                              _selected_paths=Mock(side_effect=AssertionError("Implicit selection")))
        with patch("sage.ui.application.threading.Thread") as thread:
            thread.side_effect = lambda **kwargs: SimpleNamespace(start=kwargs["target"])
            SageApplication._answer(app, "Which items can I delete in C drive?")
        self.assertEqual(conversation.answer.call_args.args, ("Which items can I delete in C drive?",))

    def test_explicit_file_action_target_does_not_depend_on_tree_selection(self):
        result = SimpleNamespace(root_frn=5, path_for=lambda frn: "C:\\Users\\Ada\\Downloads\\archive.zip")
        app = SimpleNamespace(_result=result, tree_panel=SimpleNamespace(result=result), _action_frns=[23])
        self.assertEqual(SageApplication._selected_paths(app), ["C:\\Users\\Ada\\Downloads\\archive.zip"])
        app._action_frns.clear()
        self.assertEqual(SageApplication._selected_paths(app), [])


class OperationPolicyTests(unittest.TestCase):
    def test_protected_and_managed_cleanup_paths_never_reach_shell(self):
        for path in ("C:\\Program Files\\Git", "C:\\Users", "C:\\Users\\Ada\\AppData\\Local\\pip\\cache"):
            with self.subTest(path=path), patch("sage.platform.windows.file_operations.ctypes.WinDLL") as shell:
                with self.assertRaises(ValueError):
                    shell_file_operation("recycle", [path])
                shell.assert_not_called()

    def test_live_reparse_ancestor_and_missing_file_are_rejected(self):
        def metadata(path):
            return SimpleNamespace(st_file_attributes=0x400 if path.endswith("Downloads") else 0)
        with patch("sage.platform.windows.file_operations.os.lstat", side_effect=metadata):
            with self.assertRaisesRegex(ValueError, "reparse"):
                validate_mutation(["C:\\Users\\Ada\\Downloads\\archive.zip"])
        with patch("sage.platform.windows.file_operations.os.lstat", side_effect=FileNotFoundError("Gone")):
            with self.assertRaises(FileNotFoundError):
                validate_mutation(["C:\\Users\\Ada\\Downloads\\archive.zip"])

    def test_personal_file_gets_consequence_assessment_without_mutation(self):
        with patch("sage.platform.windows.file_operations.os.lstat", return_value=SimpleNamespace(st_file_attributes=0)):
            assessments = validate_mutation(["C:\\Users\\Ada\\Downloads\\archive.zip"])
        self.assertEqual(assessments[0]["risk"], "review")
        self.assertIn("Removing it loses", assessments[0]["guidance"])


if __name__ == "__main__":
    unittest.main()
