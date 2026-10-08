import unittest
import json
import io
from unittest.mock import patch

from sage.config import FILE_ATTRIBUTE_DIRECTORY
from sage.models import FileRecord, VolumeInfo
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from sage.services.local_ai import ConversationAssistant, OllamaClient
from sage.services.retrieval import ScanEvidence, assess_path


def sample_scan():
    records = {
        5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY),
        10: FileRecord(10, 5, "Program Files (x86)", FILE_ATTRIBUTE_DIRECTORY),
        11: FileRecord(11, 10, "example.exe", 0, logical_size=20, allocated_size=24, size_ready=True),
        20: FileRecord(20, 5, "Users", FILE_ATTRIBUTE_DIRECTORY),
        21: FileRecord(21, 20, "Ada", FILE_ATTRIBUTE_DIRECTORY),
        22: FileRecord(22, 21, "Downloads", FILE_ATTRIBUTE_DIRECTORY),
        23: FileRecord(23, 22, "archive.zip", 0, logical_size=100, allocated_size=104, size_ready=True),
    }
    result = build_scan_result(VolumeInfo("C:", "System", "NTFS"), records)
    calculate_directory_sizes(result)
    result.sized_files = 2
    return result


class RetrievalTests(unittest.TestCase):
    def test_protected_and_review_paths(self):
        self.assertEqual(assess_path("C:\\Program Files (x86)\\Example", "C:")["risk"], "protected")
        self.assertEqual(assess_path("C:\\Windows\\System32", "C:")["risk"], "protected")
        self.assertEqual(assess_path("E:\\Program Files\\Example", "C:")["risk"], "protected")
        self.assertEqual(assess_path("C:\\", "C:")["risk"], "protected")
        self.assertEqual(assess_path("C:\\Users\\Ada\\Downloads", "C:")["risk"], "review")
        self.assertEqual(assess_path("E:\\Projects", "C:")["risk"], "unknown")

    def test_largest_search_and_inspection_use_current_scan(self):
        evidence = ScanEvidence(sample_scan())
        self.assertEqual(evidence.overview()["allocated_bytes"], 128)
        self.assertEqual(evidence.largest("files")["items"][0]["path"], "C:\\Users\\Ada\\Downloads\\archive.zip")
        self.assertEqual(evidence.search("example")["items"][0]["cleanup_risk"]["risk"], "protected")
        self.assertEqual(evidence.inspect("C:\\Program Files (x86)")["cleanup_risk"]["risk"], "protected")
        self.assertIn("error", evidence.inspect("E:\\Private.txt"))

    def test_question_resolves_git_and_keeps_duplicate_folders(self):
        result = sample_scan()
        result.records[30] = FileRecord(30, 5, "Git", FILE_ATTRIBUTE_DIRECTORY)
        result.records[31] = FileRecord(31, 22, "Git", FILE_ATTRIBUTE_DIRECTORY)
        result.records[32] = FileRecord(32, 22, "git-guide.txt", 0)
        evidence = ScanEvidence(result)
        items = evidence.mentioned("Should I delete git?")["items"]
        self.assertEqual({item["path"] for item in items}, {"C:\\Git", "C:\\Users\\Ada\\Downloads\\Git"})
        self.assertEqual(evidence.mentioned("Hey")["items"], [])
        self.assertEqual(evidence.mentioned("How much space does it use?")["items"], [])
        result.records[33] = FileRecord(33, 5, "Space", FILE_ATTRIBUTE_DIRECTORY)
        quoted = ScanEvidence(result).mentioned('Should I delete "Space"?')["items"]
        self.assertEqual(quoted[0]["path"], "C:\\Space")

    def test_sizes_include_preformatted_units(self):
        result = sample_scan()
        result.records[23].allocated_size = 1024
        item = ScanEvidence(result).inspect("C:\\Users\\Ada\\Downloads\\archive.zip")
        self.assertEqual(item["allocated_display"], "1.0 KB")


class ConversationTests(unittest.TestCase):
    def test_greeting_and_counts_do_not_call_model_or_retrieve_tree(self):
        def forbidden(_payload):
            self.fail("Simple conversation must be immediate")
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=forbidden))
        with patch.object(assistant.evidence, "mentioned", side_effect=AssertionError("Unneeded retrieval")):
            self.assertIn("Hey!", assistant.answer("Hey"))
            self.assertNotIn("overview", assistant.history[-1]["content"].lower())
            self.assertIn("2 files", assistant.answer("How many files?"))
        self.assertIn("Hey!", ConversationAssistant(client=OllamaClient(transport=forbidden)).answer("Hey"))

    def test_stream_delivers_text_before_completion_and_records_metrics(self):
        chunks = [
            {"message": {"content": "**Total"}, "done": False},
            {"message": {"content": " Size:** 354 MB"}, "done": False},
            {"message": {"content": ""}, "done": True, "eval_count": 8},
        ]
        response = io.BytesIO(b"\n".join(json.dumps(chunk).encode() for chunk in chunks))
        tokens = []
        with patch("sage.services.local_ai.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            client = OllamaClient()
            message = client.chat([{"role": "user", "content": "size?"}], tokens.append)
        self.assertEqual(tokens, ["**Total", " Size:** 354 MB"])
        self.assertEqual(message["content"], "".join(tokens))
        self.assertEqual(client.last_metrics["eval_count"], 8)

    def test_incomplete_stream_does_not_silently_succeed(self):
        response = io.BytesIO(b'{"message":{"content":"partial"},"done":false}\n')
        with patch("sage.services.local_ai.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(RuntimeError, "before completion"):
                OllamaClient().chat([])

    def test_timeout_does_not_claim_ollama_is_disconnected(self):
        with patch("sage.services.local_ai.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("timed out")
            assistant = ConversationAssistant(sample_scan())
            answer = assistant.answer("Is the archive worth keeping?")
        self.assertIn("did not finish within 180 seconds", answer)
        self.assertNotIn("No external AI service is connected", answer)
        self.assertIn("cannot determine which files you should delete", answer)

    def test_protected_delete_question_never_reaches_model(self):
        def forbidden(_payload):
            self.fail("Protected deletion must not be delegated to the model")

        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=forbidden))
        answer = assistant.answer("Can I delete C:\\Program Files (x86)?")
        self.assertIn("Do not directly delete", answer)
        self.assertIn("Windows Uninstall", answer)

    def test_grounding_uses_one_model_call_and_preserves_followup_context(self):
        calls = []

        def fake_transport(payload):
            calls.append(payload)
            return {"message": {"role": "assistant", "content": "The archive at C:\\Users\\Ada\\Downloads\\archive.zip is 104 bytes allocated. Review it before removal."}}

        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=fake_transport))
        answer = assistant.answer("How large is the archive?")
        self.assertIn("104 bytes", answer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["keep_alive"], "10m")
        self.assertNotIn("tools", calls[0])
        self.assertTrue(calls[0]["stream"])
        self.assertIn("archive.zip", calls[0]["messages"][-1]["content"])
        assistant.answer("What about it?")
        self.assertIn("How large is the archive?", str(calls[1]["messages"]))
        self.assertIn("previous_items", calls[1]["messages"][-1]["content"])

    def test_named_git_folder_is_grounded_before_first_model_call(self):
        result = sample_scan()
        result.records[30] = FileRecord(30, 5, "Git", FILE_ATTRIBUTE_DIRECTORY)
        result.children[5].append(30)
        payloads = []
        def fake(payload):
            payloads.append(payload)
            return {"message": {"content": "I found C:\\Git. Let's check its purpose before removing it."}}
        assistant = ConversationAssistant(result, OllamaClient(transport=fake))
        answer = assistant.answer("Should I delete git?")
        context = json.loads(payloads[0]["messages"][-1]["content"].split("(JSON): ")[1])
        self.assertEqual(context["matched_items"][0]["path"], "C:\\Git")
        self.assertNotIn("largest_folders", context)
        self.assertIn("C:\\Git", answer)
        calls_before = len(payloads)
        reply = assistant.answer("How much space does it use?")
        self.assertIn("C:\\Git", reply)
        self.assertEqual(len(payloads), calls_before)
        self.assertNotIn("Users", reply)

    def test_unavailable_model_falls_back_to_basic_evidence(self):
        def unavailable(_payload):
            raise RuntimeError("Local Ollama is unavailable")

        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=unavailable))
        answer = assistant.answer("Give me an overview of files")
        self.assertIn("Local Ollama is unavailable", answer)
        self.assertIn("Files: 2", answer)


if __name__ == "__main__":
    unittest.main()
