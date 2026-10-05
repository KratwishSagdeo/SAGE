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
            answer = assistant.answer("Give me an overview")
        self.assertIn("did not finish within 180 seconds", answer)
        self.assertNotIn("No external AI service is connected", answer)
        self.assertIn("Basic scan answer", answer)

    def test_protected_delete_question_never_reaches_model(self):
        def forbidden(_payload):
            self.fail("Protected deletion must not be delegated to the model")

        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=forbidden))
        answer = assistant.answer("Can I delete C:\\Program Files (x86)?")
        self.assertIn("Do not directly delete", answer)
        self.assertIn("Installed apps", answer)

    def test_grounding_uses_one_model_call_and_preserves_followup_context(self):
        calls = []

        def fake_transport(payload):
            calls.append(payload)
            return {"message": {"role": "assistant", "content": "The archive at C:\\Users\\Ada\\Downloads\\archive.zip is 104 bytes allocated. Review it before removal."}}

        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=fake_transport))
        answer = assistant.answer("How large is archive.zip?")
        self.assertIn("104 bytes", answer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["keep_alive"], "10m")
        self.assertNotIn("tools", calls[0])
        self.assertTrue(calls[0]["stream"])
        self.assertIn("archive.zip", calls[0]["messages"][-1]["content"])
        assistant.answer("What about it?")
        self.assertIn("How large is archive.zip?", str(calls[1]["messages"]))
        self.assertIn("previous_items", calls[1]["messages"][-1]["content"])

    def test_named_git_assessment_never_needs_model_and_followup_uses_target(self):
        result = sample_scan()
        result.records[30] = FileRecord(30, 5, "Git", FILE_ATTRIBUTE_DIRECTORY)
        payloads = []
        def fake(payload):
            payloads.append(payload)
            return {"message": {"content": "I found C:\\Git. Let's check its purpose before removing it."}}
        assistant = ConversationAssistant(result, OllamaClient(transport=fake))
        answer = assistant.answer("Should I delete git?")
        self.assertEqual(payloads, [])
        self.assertIn("C:\\Git", answer)
        calls_before = len(payloads)
        reply = assistant.answer("How much space does it use?")
        self.assertIn("C:\\Git", reply)
        self.assertEqual(len(payloads), calls_before)
        self.assertNotIn("Users", reply)

    def test_transcript_duplicate_hp_requires_confirmation_before_advice(self):
        result = sample_scan()
        result.records[30] = FileRecord(30, 5, "HP", FILE_ATTRIBUTE_DIRECTORY)
        result.records[31] = FileRecord(31, 10, "HP", FILE_ATTRIBUTE_DIRECTORY)
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("Clarification must be local")))
        reply = assistant.answer("Shoild I delete this HP")
        self.assertIn("Which item", reply)
        self.assertIsNone(assistant._confirmed_path)
        reply = assistant.answer("1")
        self.assertIn("C:\\HP", reply)
        self.assertNotIn("Program Files", reply)
        self.assertEqual(assistant._confirmed_path, "C:\\HP")

    def test_selected_item_cannot_override_drive_cleanup_or_named_target(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Cleanup must be local")))
        reply = assistant.answer("Which items can I delete in C drive itself?", ("C:\\Program Files (x86)",))
        self.assertIn("archive.zip", reply)
        self.assertNotIn("Do not directly delete", reply)
        reply = assistant.answer("Should I delete archive.zip?", ("C:\\Program Files (x86)",))
        self.assertIn("archive.zip", reply)
        self.assertNotIn("Program Files", reply)

    def test_here_without_confirmed_scope_asks_even_with_tree_selection(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Clarification must be local")))
        self.assertIn("Which file, folder or drive", assistant.answer("What should I delelet here?", ("C:\\Users",)))

    def test_scope_answer_continues_original_cleanup_request(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Cleanup must be local")))
        self.assertIn("Which scope", assistant.answer("What should I delete?"))
        self.assertIn("archive.zip", assistant.answer("C:"))
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Cleanup must be local")))
        assistant.answer("What should I delete here?")
        self.assertIn("archive.zip", assistant.answer("C:"))

    def test_new_question_during_clarification_keeps_its_own_intent(self):
        result = sample_scan()
        result.records[30] = FileRecord(30, 5, "HP", FILE_ATTRIBUTE_DIRECTORY)
        result.records[31] = FileRecord(31, 10, "HP", FILE_ATTRIBUTE_DIRECTORY)
        payloads = []
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda payload: payloads.append(payload) or {"message": {"content": "Size is pending."}}))
        assistant.answer("Should I delete HP?")
        assistant.answer("How large is C:\\HP?")
        self.assertEqual(len(payloads), 1)
        self.assertIn("How large is", payloads[0]["messages"][-1]["content"])

    def test_path_followed_by_question_does_not_consume_question(self):
        payloads = []
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda payload: payloads.append(payload) or {"message": {"content": "Observed children only."}}))
        assistant.answer("C:\\Users What does this consist of?")
        context = json.loads(payloads[0]["messages"][-1]["content"].split("(JSON): ")[1])
        self.assertEqual(context["scope"], "C:\\Users")
        self.assertIn("Ada", str(context["direct_children"]))
        self.assertFalse(context["contents_read"])

    def test_missing_explicit_path_never_falls_back_to_selected_or_old_item(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Clarification must be local")))
        assistant.answer("Should I delete archive.zip?")
        reply = assistant.answer("Should I delete C:\\Users\\Ada\\Missing?", ("C:\\Users\\Ada\\Downloads\\archive.zip",))
        self.assertIn("could not resolve", reply)
        self.assertNotIn("Assessment for", reply)

    def test_partial_match_needs_confirmation(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Clarification must be local")))
        self.assertIn("partial name match", assistant.answer("Should I delete archive?"))
        self.assertIsNone(assistant._confirmed_path)
        self.assertIn("archive.zip", assistant.answer("1"))

    def test_new_drive_scope_replaces_confirmed_folder_and_outside_drive_clarifies(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Cleanup must be local")))
        assistant.answer("Should I delete C:\\Program Files (x86)?")
        self.assertIn("archive.zip", assistant.answer("How can I free up space in C drive?"))
        self.assertEqual(assistant._confirmed_path, "C:\\")
        self.assertIn("Scan E:", assistant.answer("What should I delete in E drive?"))

    def test_named_item_with_drive_scope_is_not_mistaken_for_drive_root(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Assessment must be local")))
        self.assertIn("archive.zip", assistant.answer("Should I delete archive.zip in C drive?"))
        self.assertEqual(assistant._confirmed_path, "C:\\Users\\Ada\\Downloads\\archive.zip")

    def test_missing_new_path_invalidates_old_followup_target(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Clarification must be local")))
        assistant.answer("Should I delete archive.zip?")
        assistant.answer("Should I delete C:\\Missing?")
        self.assertIn("Which file, folder or drive", assistant.answer("Should I delete it?"))

    def test_empty_candidates_do_not_claim_entire_drive_has_no_safe_items(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Cleanup must be local")))
        reply = assistant.answer("What should I delete in C:\\Program Files (x86)?")
        self.assertIn("not identified a candidate", reply)
        self.assertIn("does not mean nothing", reply)
        self.assertNotIn("no safe items", reply)

    def test_pip_cache_and_logs_get_distinct_evidence_limits(self):
        self.assertEqual(assess_path("C:\\Users\\Ada\\AppData\\Local\\pip\\cache")["risk"], "managed_cleanup")
        self.assertEqual(assess_path("C:\\Users\\Ada\\AppData\\Local\\Programs\\IDE")["risk"], "protected")
        self.assertIn("contents have not been read", assess_path("C:\\HP\\DATA\\HPOMEN.log")["guidance"])
        self.assertEqual(assess_path("C:\\Users")["risk"], "protected")

    def test_cleanup_scope_ranks_files_without_overlap_and_never_follows_links(self):
        result = sample_scan()
        result.records[30] = FileRecord(30, 22, "junction", FILE_ATTRIBUTE_DIRECTORY | 0x400)
        result.records[31] = FileRecord(31, 30, "secret.bin", 0, 9999, 9999, size_ready=True)
        result = build_scan_result(result.volume, result.records)
        calculate_directory_sizes(result)
        evidence = ScanEvidence(result)
        self.assertEqual([item["path"] for item in evidence.cleanup_candidates("C:\\Users")["items"]], ["C:\\Users\\Ada\\Downloads\\archive.zip"])
        self.assertEqual(evidence.cleanup_candidates("C:\\Program Files (x86)")["items"], [])

    def test_quoted_path_preserves_question_words_in_filename(self):
        result = sample_scan()
        result.records[30] = FileRecord(30, 22, "what is this.txt", 0)
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("Assessment must be local")))
        self.assertIn("what is this.txt", assistant.answer('Can I delete "C:\\Users\\Ada\\Downloads\\what is this.txt"?'))
        self.assertEqual(assistant._confirmed_path, "C:\\Users\\Ada\\Downloads\\what is this.txt")

    def test_unavailable_model_falls_back_to_basic_evidence(self):
        def unavailable(_payload):
            raise RuntimeError("Local Ollama is unavailable")

        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=unavailable))
        answer = assistant.answer("Give me an overview of files")
        self.assertIn("Local Ollama is unavailable", answer)
        self.assertIn("Files: 2", answer)


if __name__ == "__main__":
    unittest.main()
