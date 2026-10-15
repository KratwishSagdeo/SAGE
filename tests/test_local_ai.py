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
        self.assertIn("do not delete program files", answer.casefold())
        self.assertNotIn("cannot verify", answer)
        self.assertNotIn("Game libraries", answer)

    def test_grounding_uses_one_model_call_and_preserves_followup_context(self):
        calls = []

        def fake_transport(payload):
            calls.append(payload)
            return {"message": {"role": "assistant", "content": "The archive at C:\\Users\\Ada\\Downloads\\archive.zip is 104 bytes allocated. Review it before removal."}}

        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=fake_transport))
        answer = assistant.answer("How large is archive.zip?")
        self.assertIn("104 B", answer)
        self.assertIn("archive.zip", answer)
        self.assertEqual(calls, [])
        reply = assistant.answer("What about it?")
        self.assertIn("archive.zip", reply)
        self.assertNotIn("Program Files", reply)
        self.assertEqual(calls, [])

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
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("The choice is answered from the scan")))
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
        self.assertIn("no folder named `archive.zip` in `C:\\Program Files (x86)`", reply)
        self.assertIn("C:\\Users\\Ada\\Downloads\\archive.zip", reply)
        chosen = assistant.answer("1")
        self.assertIn("archive.zip", chosen)
        self.assertNotIn("Do not directly delete", chosen)

    def test_clicked_folder_is_the_subject_without_typing_its_path(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("A clicked folder is answered from the scan")))
        reply = assistant.answer("What is this?", ("C:\\Users",))
        self.assertIn("C:\\Users", reply)
        self.assertIn("Ada", reply)
        self.assertIn("not permission to delete", reply)
        self.assertEqual(assistant._confirmed_path, "C:\\Users")

    def test_off_topic_question_does_not_explain_the_selected_folder(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Off-topic chat is local")))
        reply = assistant.answer("am i sexy?", ("C:\\Users",))
        self.assertIn("not a question about this drive", reply.casefold())
        self.assertNotIn("C:\\Users", reply)
        self.assertNotIn("node_modules", reply)
        self.assertNotIn("System Volume Information", reply)
        missing = assistant.answer("what is the weather", ("C:\\Users",))
        self.assertIn("not a question about this drive", missing.casefold())
        self.assertNotIn("C:\\Users", missing)

    def test_chat_never_searches_locale_folders_or_people_names(self):
        result = sample_scan()
        result.records[40] = FileRecord(40, 22, "am", FILE_ATTRIBUTE_DIRECTORY)
        result = build_scan_result(result.volume, result.records)
        calculate_directory_sizes(result)
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("Chat must not search the scan")))
        for question in (
            "Am i looking hot",
            "are you idiot",
            "was osama bin laden a good guy",
            "is arayn iyer good guy?",
            "is being gay good?",
            "am i a good guy",
            "am i sexy",
        ):
            reply = assistant.answer(question, ("C:\\Users",))
            self.assertIn("not a question about this drive", reply.casefold(), question)
            self.assertNotIn("Which item", reply, question)
            self.assertNotIn("full path", reply.casefold(), question)
            self.assertNotIn("\\am", reply, question)
            self.assertNotIn("C:\\Users", reply, question)

    def test_name_missing_from_selected_folder_offers_matches_on_the_drive(self):
        result = sample_scan()
        result.records[40] = FileRecord(40, 5, "ProgramData", FILE_ATTRIBUTE_DIRECTORY)
        result = build_scan_result(result.volume, result.records)
        calculate_directory_sizes(result)
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("The choice is answered locally")))
        reply = assistant.answer("Can I delete program data?", ("C:\\Program Files (x86)",))
        self.assertIn("no folder named `program data` in `C:\\Program Files (x86)`", reply)
        self.assertIn("Are you talking about one of these?", reply)
        self.assertIn("C:\\ProgramData", reply)
        self.assertNotIn("could not find an exact match", reply)
        chosen = assistant.answer("1")
        self.assertIn("C:\\ProgramData", chosen)
        self.assertIn("Do not directly delete", chosen)
        self.assertIn("What to do instead", chosen)
        self.assertIn("ms-settings:appsfeatures", {link["target"] for link in assistant.last_links})

    def test_yes_accepts_the_only_offered_match_and_ignores_the_clicked_folder(self):
        result = sample_scan()
        result.records[40] = FileRecord(40, 5, "ProgramData", FILE_ATTRIBUTE_DIRECTORY)
        result = build_scan_result(result.volume, result.records)
        calculate_directory_sizes(result)
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("Confirmation stays local")))
        assistant.answer("Can I delete program data?", ("C:\\Program Files (x86)",))
        chosen = assistant.answer("yess", ("C:\\Recovery",))
        self.assertIn("C:\\ProgramData", chosen)
        self.assertNotIn("Recovery", chosen)

    def test_docs_means_the_real_documents_folder(self):
        result = sample_scan()
        result.records[40] = FileRecord(40, 5, "Documents and Settings", FILE_ATTRIBUTE_DIRECTORY)
        result.records[41] = FileRecord(41, 21, "Documents", FILE_ATTRIBUTE_DIRECTORY)
        result = build_scan_result(result.volume, result.records)
        calculate_directory_sizes(result)
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("Documents answer stays local")))
        reply = assistant.answer("Should I delete this docs?")
        self.assertIn("C:\\Users\\Ada\\Documents", reply)
        self.assertNotIn("Documents and Settings", reply)
        self.assertIn("You can move it to", reply)

    def test_folder_with_nothing_to_remove_says_so(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Explanation stays local")))
        reply = assistant.answer("What is this?", ("C:\\Program Files (x86)",))
        self.assertIn("**Decision**", reply)
        self.assertIn("Decision: do not delete", reply)
        self.assertNotIn("verified delete list", reply)
        self.assertEqual(assistant.last_links[0]["label"], "Open Installed apps")

    def test_here_with_a_clicked_folder_assesses_that_folder(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: self.fail("Cleanup must be local")))
        reply = assistant.answer("What should I delelet here?", ("C:\\Users",))
        self.assertIn("C:\\Users", reply)
        self.assertIn("archive.zip", reply)
        self.assertNotIn("Which file, folder or drive", reply)

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
        self.assertIn("Which item", assistant.answer("Should I delete HP?"))
        reply = assistant.answer("How large is C:\\HP?")
        self.assertIn("C:\\HP", reply)
        self.assertIn("allocated", reply)
        self.assertNotIn("Which item", reply)
        self.assertEqual(payloads, [])

    def test_path_followed_by_question_does_not_consume_question(self):
        payloads = []
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda payload: payloads.append(payload) or {"message": {"content": "Observed children only."}}))
        reply = assistant.answer("C:\\Users What does this consist of?")
        self.assertEqual(payloads, [])
        self.assertIn("C:\\Users", reply)
        self.assertIn("Ada", reply)
        self.assertIn("not permission to delete", reply)

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
        self.assertIn("Decision: do not delete", reply)
        self.assertIn("C:\\Program Files (x86)", reply)
        self.assertNotIn("no safe items", reply)
        self.assertNotIn("Name a child folder", reply)

    def test_pip_cache_and_logs_get_distinct_evidence_limits(self):
        self.assertEqual(assess_path("C:\\Users\\Ada\\AppData\\Local\\pip\\cache")["risk"], "managed_cleanup")
        self.assertEqual(assess_path("C:\\Users\\Ada\\AppData\\Local\\Programs\\IDE")["risk"], "protected")
        self.assertIn("contents have not been read", assess_path("C:\\HP\\DATA\\HPOMEN.log")["guidance"])
        self.assertEqual(assess_path("C:\\Users")["risk"], "protected")
        self.assertEqual(assess_path("C:\\Documents and Settings")["risk"], "protected")
        self.assertIn("compatibility link", assess_path("C:\\Documents and Settings")["guidance"])

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

    def test_project_on_d_is_decided_by_the_model_from_rebuildable_caches(self):
        records = {
            5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY),
            10: FileRecord(10, 5, "bidding", FILE_ATTRIBUTE_DIRECTORY),
            11: FileRecord(11, 10, "Bidding_App", FILE_ATTRIBUTE_DIRECTORY),
            12: FileRecord(12, 11, "node_modules", FILE_ATTRIBUTE_DIRECTORY),
            13: FileRecord(13, 11, "package.json", 0, logical_size=100, allocated_size=4096, size_ready=True),
        }
        result = build_scan_result(VolumeInfo("D:", "Data", "NTFS"), records)
        calculate_directory_sizes(result)
        result.sizes_complete = True
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("Drive cleanup is answered from the scan")))
        reply = assistant.answer("Which files are safe to delete on the entire D drive?")
        self.assertNotIn("only for C:", reply)
        self.assertNotIn("did not return a text answer", reply)
        self.assertIn("node_modules", reply)
        self.assertIn("npm install", reply)
        self.assertTrue(assistant.last_suggestions)
        self.assertIn("node_modules", assistant.last_suggestions[0].path)

    def test_plain_drive_phrases_mean_the_scanned_drive(self):
        records = {
            5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY),
            10: FileRecord(10, 5, "bidding", FILE_ATTRIBUTE_DIRECTORY),
            11: FileRecord(11, 10, "node_modules", FILE_ATTRIBUTE_DIRECTORY),
            12: FileRecord(12, 10, "package.json", 0, 100, 4096, size_ready=True),
        }
        result = build_scan_result(VolumeInfo("D:", "Data", "NTFS"), records)
        calculate_directory_sizes(result)
        result.sizes_complete = True
        questions = (
            "what should i delete here?",
            "what should i delete in the entire D drive",
            "what can i delete in this drive",
            "D",
        )
        for question in questions:
            assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("Drive phrases are answered from the scan")))
            reply = assistant.answer(question)
            self.assertIn("node_modules", reply, question)
            self.assertNotIn("could not find", reply.casefold(), question)
            self.assertNotIn("Which file, folder or drive", reply, question)

    def test_vendor_folder_on_c_must_not_be_moved(self):
        result = sample_scan()
        result.records[40] = FileRecord(40, 5, "hp", FILE_ATTRIBUTE_DIRECTORY)
        result.records[41] = FileRecord(41, 40, "HPQWare", FILE_ATTRIBUTE_DIRECTORY)
        result.records[42] = FileRecord(42, 41, "setup.exe", 0, 1_000_000, 1_000_000, size_ready=True)
        result = build_scan_result(result.volume, result.records)
        calculate_directory_sizes(result)
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("Move answers stay on the scan")))
        reply = assistant.answer("is it safe to move hp folder to somewhere else?")
        self.assertIn("C:\\hp", reply)
        self.assertIn("Do not move", reply)
        self.assertNotIn("Safe to delete", reply)
        self.assertNotIn("node_modules", reply)
        self.assertNotIn("Leave these in place", reply)

    def test_move_some_folder_into_users_asks_which_source(self):
        result = sample_scan()
        result.records[40] = FileRecord(40, 5, "hp", FILE_ATTRIBUTE_DIRECTORY)
        result.records[41] = FileRecord(41, 40, "setup.exe", 0, 1_000_000, 1_000_000, size_ready=True)
        result = build_scan_result(result.volume, result.records)
        calculate_directory_sizes(result)
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("Move into Users stays on the scan")))
        reply = assistant.answer("can i move some folder inside users?")
        self.assertIn("Which folder", reply)
        self.assertIn("into `C:\\Users`", reply)
        self.assertNotIn("Do not move a user profile folder", reply)
        self.assertIn("C:\\hp", reply)
        self.assertIn("1. ", reply)
        chosen = assistant.answer("1")
        self.assertNotIn("Which folder", chosen)
        self.assertIn("C:\\Users", chosen)
        self.assertNotIn("Do not move a user profile folder", chosen)
        self.assertTrue("does not break Windows" in chosen or "does not change how Windows" in chosen)

    def test_programdata_children_get_their_own_move_answers(self):
        result = sample_scan()
        result.records[40] = FileRecord(40, 5, "ProgramData", FILE_ATTRIBUTE_DIRECTORY)
        result.records[41] = FileRecord(41, 40, "USOShared", FILE_ATTRIBUTE_DIRECTORY)
        result.records[42] = FileRecord(42, 40, "Mozilla", FILE_ATTRIBUTE_DIRECTORY)
        result.records[43] = FileRecord(43, 41, "log.dat", 0, 80 * 1024 * 1024, 80 * 1024 * 1024, size_ready=True)
        result.records[44] = FileRecord(44, 42, "data.dat", 0, 50 * 1024 * 1024, 50 * 1024 * 1024, size_ready=True)
        result = build_scan_result(result.volume, result.records)
        calculate_directory_sizes(result)
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("Move answers stay on the scan")))
        shared = assistant.answer("is it safe to move C:\\ProgramData\\USOShared?")
        self.assertIn("Windows Update", shared)
        self.assertNotIn("Steam", shared)
        self.assertNotIn("Epic", shared)
        mozilla = assistant.answer("can I move C:\\ProgramData\\Mozilla somewhere else?")
        self.assertIn("Firefox", mozilla)
        self.assertNotIn("Windows Update Orchestrator", mozilla)
        self.assertNotIn("Steam", mozilla)
        inside = assistant.answer("can I move some other files in a particular folder such as Program data?")
        self.assertIn("C:\\ProgramData", inside)
        self.assertIn("USOShared", inside)
        self.assertIn("Mozilla", inside)
        self.assertIn("Windows Update", inside)
        self.assertIn("Firefox", inside)
        self.assertNotIn("Game libraries", inside)

    def test_program_files_cleanup_does_not_reuse_the_move_speech(self):
        result = sample_scan()
        result.records[50] = FileRecord(50, 5, "Program Files", FILE_ATTRIBUTE_DIRECTORY)
        result.records[51] = FileRecord(51, 50, "Git", FILE_ATTRIBUTE_DIRECTORY)
        result.records[52] = FileRecord(52, 51, "tmp", FILE_ATTRIBUTE_DIRECTORY)
        result = build_scan_result(result.volume, result.records)
        calculate_directory_sizes(result)
        assistant = ConversationAssistant(result, OllamaClient(transport=lambda _: self.fail("Cleanup stays on the scan")))
        reply = assistant.answer("what can i delete inside program files?")
        self.assertIn("Installed apps", reply)
        self.assertNotIn("Can it be moved", reply)
        self.assertNotIn("Git\\tmp", reply)
        self.assertNotIn("Epic", reply)

    def test_unavailable_model_falls_back_to_basic_evidence(self):
        def unavailable(_payload):
            raise RuntimeError("Local Ollama is unavailable")

        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=unavailable))
        answer = assistant.answer("Give me an overview of files")
        self.assertIn("Local Ollama is unavailable", answer)
        self.assertIn("Files: 2", answer)


if __name__ == "__main__":
    unittest.main()
