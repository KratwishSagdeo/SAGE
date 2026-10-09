"""Main two-pane application and background job coordination."""

from __future__ import annotations

import queue
import os
import subprocess
import threading
import time
import tkinter as tk
from collections.abc import Callable
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import __version__
from ..config import APP_NAME, APP_SUBTITLE, MIN_WINDOW_SIZE, WINDOW_SIZE
from ..models import ScanResult, VolumeInfo
from ..platform.windows.drives import list_ntfs_volumes
from ..platform.windows.file_operations import delete_permanently, empty_recycle_bin, shell_file_operation
from ..services.cleanup import CleanupSuggestion, cleanup_report
from ..services.evidence import EvidenceAssistant, format_bytes
from ..services.local_ai import ConversationAssistant, OllamaClient
from ..services.scanner import analyze_sizes, scan_ntfs
from .assistant_panel import AssistantPanel
from .styles import configure_styles
from .tree_panel import TreePanel


class _ScanProgress:
    """Keep the established count callback while exposing optional detail."""

    def __init__(self, events: queue.SimpleQueue[tuple], generation: int) -> None:
        self.events = events
        self.generation = generation

    def __call__(self, count: int, fraction: float | None = None) -> None:
        self.events.put(("mft_progress", self.generation, count, fraction))

    def on_details(self, count: int, fraction: float | None) -> None:
        self.events.put(("mft_progress", self.generation, count, fraction))

    def on_stage(self, label: str) -> None:
        self.events.put(("mft_stage", self.generation, label))


class SageApplication(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__} â€” {APP_SUBTITLE}")
        self.geometry(WINDOW_SIZE)
        self.minsize(*MIN_WINDOW_SIZE)
        configure_styles(ttk.Style(self))

        self._events: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self._generation = 0
        self._scan_cancel = threading.Event()
        self._size_cancel = threading.Event()
        self._result: ScanResult | None = None
        self._assistant = EvidenceAssistant()
        self._conversation = ConversationAssistant()
        self._assistant_busy = False
        self._assistant_request_id = 0
        self._volumes: dict[str, VolumeInfo] = {}
        self._clipboard_paths: list[str] = []
        self._clipboard_action = "copy"
        self._file_operation_running = False
        self._last_tree_refresh = 0.0
        self._pending_size_completion: ScanResult | None = None
        self._scan_started_at = 0.0
        self._size_started_at = 0.0
        self._mft_elapsed = 0.0

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(50, self._drain_events)
        self.after(200, self._bring_to_front)
        self.after(300, self._warm_ai)
        self._load_volumes()

    def _warm_ai(self) -> None:
        self.assistant_panel.set_status("Loading local AI in the backgroundâ€¦")

        def worker() -> None:
            try:
                OllamaClient().chat([])
                status = "Local AI ready"
            except RuntimeError:
                status = "Local AI unavailable â€” basic scan answers still work"
            self._events.put(("ai_ready", self._generation, status))

        threading.Thread(target=worker, daemon=True, name="sage-ai-warmup").start()

    def _bring_to_front(self) -> None:
        """Make the post-UAC window visible instead of leaving it behind the caller."""
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def _build_ui(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(18, 12))
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="App.TLabel").pack(side="left")
        ttk.Label(header, text=APP_SUBTITLE, style="Subtitle.TLabel").pack(
            side="left", padx=(12, 0), pady=(4, 0)
        )
        self.scan_button = ttk.Button(header, text="Scan drive", style="Accent.TButton", command=self._start_scan)
        self.scan_button.pack(side="right")
        self.drive_var = tk.StringVar()
        self.drive_box = ttk.Combobox(
            header, textvariable=self.drive_var, state="readonly", width=26
        )
        self.drive_box.pack(side="right", padx=(8, 10))
        ttk.Label(header, text="Drive", style="HeaderField.TLabel").pack(side="right")
        ttk.Separator(self, orient="horizontal").pack(fill="x")

        progress_row = ttk.Frame(self, padding=(18, 10, 18, 10))
        progress_row.pack(fill="x")
        self.progress_phase = tk.StringVar(value="Ready to scan")
        self.progress_detail = tk.StringVar(value="")
        ttk.Label(progress_row, textvariable=self.progress_phase, style="Muted.TLabel", width=26).pack(side="left")
        ttk.Label(progress_row, textvariable=self.progress_detail, style="Muted.TLabel", width=24, anchor="e").pack(side="right")
        self.progress_bar = ttk.Progressbar(progress_row, mode="determinate", maximum=100)
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=(8, 12))

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        left = ttk.Frame(panes, style="Card.TFrame", padding=1)
        right = ttk.Frame(panes, style="Card.TFrame", padding=1)
        panes.add(left, weight=3)
        panes.add(right, weight=2)
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)
        ttk.Label(left, text="Drive contents", style="Section.TLabel", padding=(16, 12, 16, 10)).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Separator(left, orient="horizontal").grid(row=1, column=0, sticky="ew")
        self.tree_panel = TreePanel(left)
        self.tree_panel.grid(row=2, column=0, sticky="nsew")
        self.tree_panel.tree.bind("<Button-3>", self._show_tree_menu)
        self.tree_panel.tree.bind("<Double-1>", self._open_on_double_click)
        self.tree_panel.tree.bind("<Control-c>", self._copy_selection)
        self.tree_panel.tree.bind("<Control-x>", self._cut_selection)
        self.tree_panel.tree.bind("<Control-v>", self._paste_selection)
        self.tree_panel.tree.bind("<Delete>", self._recycle_selection)
        self.tree_panel.tree.bind("<F2>", self._rename_selection)
        self._tree_menu = tk.Menu(self, tearoff=False)
        self.assistant_panel = AssistantPanel(right, self._answer)
        self.assistant_panel.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="Ready")
        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=18, pady=(2, 6))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=0, sticky="ew"
        )

    def _load_volumes(self) -> None:
        try:
            volumes = list_ntfs_volumes()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not enumerate NTFS drives.\n\n{exc}")
            return
        self._volumes = {volume.display_name: volume for volume in volumes}
        self.drive_box.configure(values=list(self._volumes))
        if volumes:
            self.drive_var.set(volumes[0].display_name)
        else:
            self.status_var.set("No fixed NTFS drives were found.")
            self.scan_button.configure(state="disabled")

    def _start_scan(self) -> None:
        if self._file_operation_running:
            return
        volume = self._volumes.get(self.drive_var.get())
        if volume is None:
            return
        self._scan_cancel.set()
        self._size_cancel.set()
        self._scan_cancel = threading.Event()
        self._size_cancel = threading.Event()
        self._generation += 1
        generation = self._generation
        self._scan_started_at = time.monotonic()
        self._result = None
        self._pending_size_completion = None
        self._assistant = EvidenceAssistant()
        self._conversation = ConversationAssistant()
        if self._assistant_busy:
            self.assistant_panel.finish_reply("The drive scan changed. Ask again once the new tree is ready.")
        self._assistant_busy = False
        self.tree_panel.clear()
        self.progress_bar.stop()
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(12)
        self.progress_phase.set(f"Scanning {volume.drive} MFT")
        self.progress_detail.set("Startingâ€¦")
        self.status_var.set(f"Scanning {volume.drive} MFTâ€¦")
        self.scan_button.configure(text="Restart scan")
        threading.Thread(
            target=self._scan_worker,
            args=(generation, volume.drive, self._scan_cancel),
            daemon=True,
        ).start()

    def _scan_worker(self, generation: int, drive: str, cancel: threading.Event) -> None:
        try:
            result = scan_ntfs(
                drive,
                progress=_ScanProgress(self._events, generation),
                cancelled=cancel.is_set,
            )
            self._events.put(("scan_done", generation, result))
        except InterruptedError:
            self._events.put(("cancelled", generation))
        except Exception as exc:
            self._events.put(("error", generation, "MFT scan", exc))

    def _size_worker(
        self, generation: int, result: ScanResult, cancel: threading.Event
    ) -> None:
        try:
            analyze_sizes(
                result,
                progress=lambda current, total, errors: self._events.put(
                    ("size_progress", generation, current, total, errors)
                ),
                cancelled=cancel.is_set,
            )
            self._events.put(("size_done", generation, result))
        except InterruptedError:
            self._events.put(("cancelled", generation))
        except Exception as exc:
            self._events.put(("error", generation, "Size analysis", exc))

    def _drain_events(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                if len(event) > 1 and event[1] != self._generation:
                    continue
                kind = event[0]
                if kind.startswith("assistant_") and event[2] != self._assistant_request_id:
                    continue
                if kind == "mft_progress":
                    _, _, count, fraction = event
                    if fraction is None:
                        self.progress_bar.configure(mode="indeterminate")
                        self.progress_bar.start(12)
                        self.progress_detail.set(f"{count:,} records")
                        self.status_var.set(f"Scanning MFTâ€¦ {count:,} records")
                    else:
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate", value=fraction * 100)
                        self.progress_detail.set(f"{fraction * 100:.0f}% est. Â· {count:,} records")
                        self.status_var.set(
                            f"Scanning MFTâ€¦ {fraction * 100:.0f}% estimated, {count:,} records"
                        )
                elif kind == "mft_stage":
                    self.progress_bar.configure(mode="indeterminate")
                    self.progress_bar.start(12)
                    self.progress_phase.set("Building drive tree")
                    self.progress_detail.set("Please waitâ€¦")
                    self.status_var.set(f"{event[2]}â€¦")
                elif kind == "scan_done":
                    self._scan_finished(event[2])
                elif kind == "size_progress":
                    if self._file_operation_running:
                        continue
                    _, _, current, total, errors = event
                    percent = current / total * 100 if total else 100
                    self.progress_bar.configure(mode="determinate", value=percent)
                    self.progress_detail.set(f"{percent:.1f}% Â· {current:,}/{total:,}")
                    self.status_var.set(
                        f"Tree ready â€” measuring sizesâ€¦ {current:,}/{total:,} "
                        f"({percent:.1f}%), unreadable: {errors:,}"
                    )
                    if time.monotonic() - self._last_tree_refresh >= 0.4:
                        self.tree_panel.refresh_loaded_rows()
                        self._last_tree_refresh = time.monotonic()
                elif kind == "size_done":
                    if self._file_operation_running:
                        self._pending_size_completion = event[2]
                    else:
                        self._size_finished(event[2])
                elif kind == "assistant_answer":
                    self._assistant_busy = False
                    self.assistant_panel.finish_reply(event[3])
                    if len(event) > 4 and event[4]:
                        self.assistant_panel.add_cleanup_actions(event[4], self._delete_suggestion)
                        self._conversation.remember_suggestions(event[4])
                elif kind == "assistant_token":
                    self.assistant_panel.append_reply(event[3])
                elif kind == "assistant_status":
                    self.assistant_panel.set_status(event[3])
                elif kind == "ai_ready":
                    if not self._assistant_busy:
                        self.assistant_panel.set_status(event[2])
                elif kind == "error":
                    self._job_failed(event[2], event[3])
                elif kind == "operation_done":
                    self._operation_finished(*event[2:])
                elif kind == "operation_error":
                    self._file_operation_running = False
                    self.scan_button.configure(state="normal")
                    self.drive_box.configure(state="readonly")
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate", value=0)
                    self._job_failed("File operation", event[2])
                    if event[3]:
                        self._start_scan()
                    elif self._pending_size_completion is not None:
                        self._size_finished(self._pending_size_completion)
                        self._pending_size_completion = None
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(50, self._drain_events)

    def _scan_finished(self, result: ScanResult) -> None:
        self._mft_elapsed = time.monotonic() - self._scan_started_at
        self._size_started_at = time.monotonic()
        self._result = result
        self._assistant.set_result(result)
        if self._assistant_busy:
            self._assistant_request_id += 1
            self.assistant_panel.finish_reply("The new drive tree is ready. Please ask your question again.")
            self._assistant_busy = False
        self._conversation = ConversationAssistant(result)
        self.tree_panel.show_result(result)
        self.assistant_panel.write("SAGE", self._assistant.scan_summary())
        self.progress_bar.stop()
        self.scan_button.configure(text="Scan drive")
        if result.sizes_complete:
            self._size_finished(result)
            return
        self.progress_bar.configure(mode="determinate", value=0)
        self.progress_phase.set("Measuring file sizes")
        self.progress_detail.set("0%")
        self.status_var.set(
            f"MFT tree ready in {self._mft_elapsed:.1f}s â€” measuring file sizesâ€¦"
        )
        threading.Thread(
            target=self._size_worker,
            args=(self._generation, result, self._size_cancel),
            daemon=True,
        ).start()

    def _size_finished(self, result: ScanResult) -> None:
        if result is not self._result:
            return
        self.tree_panel.refresh_loaded_rows()
        self.progress_bar.configure(mode="determinate", value=100)
        self.progress_phase.set("Drive scan complete")
        self.progress_detail.set("100%")
        self.assistant_panel.write("SAGE", self._assistant.size_summary())
        if result.volume.drive.upper() == "C:":
            suggestions = self._conversation.cleanup_suggestions()
            self.assistant_panel.write("SAGE", cleanup_report(result, suggestions))
            self.assistant_panel.add_cleanup_actions(suggestions, self._delete_suggestion)
            self._conversation.remember_suggestions(suggestions)
        self.status_var.set(
            f"Ready â€” MFT {self._mft_elapsed:.1f}s, "
            f"sizes {time.monotonic() - self._size_started_at:.1f}s, "
            f"{len(result.records):,} records, "
            f"{result.size_errors:,} unreadable files"
        )

    def _job_failed(self, phase: str, exc: Exception) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", value=0)
        self.progress_phase.set(f"{phase} failed")
        self.progress_detail.set("â€”")
        self.scan_button.configure(text="Scan drive")
        self.status_var.set(f"{phase} failed.")
        self.assistant_panel.write("SAGE", f"{phase} failed:\n{exc}")
        messagebox.showerror(APP_NAME, f"{phase} failed.\n\n{exc}")

    def _answer(self, question: str, quoted: str = "") -> str | None:
        conversation = self._conversation
        if self._assistant_busy:
            return "I am still answering the previous question. Please try again shortly."
        self._assistant_busy = True
        self._assistant_request_id += 1
        request_id = self._assistant_request_id
        generation = self._generation
        selected_paths = tuple(self._selected_paths())
        self.assistant_panel.begin_reply()

        def worker() -> None:
            suggestions: list[CleanupSuggestion] = []
            try:
                answer = conversation.answer(
                    question, selected_paths, quoted=quoted,
                    on_token=lambda token: self._events.put(("assistant_token", generation, request_id, token)),
                    on_status=lambda status: self._events.put(("assistant_status", generation, request_id, status)),
                )
                suggestions = list(conversation.last_suggestions)
            except Exception as exc:
                answer = f"Could not answer from the current scan: {exc}"
            self._events.put(("assistant_answer", generation, request_id, answer, suggestions))

        threading.Thread(target=worker, daemon=True, name="sage-local-ai").start()
        return None

    def _selected_paths(self) -> list[str]:
        result = self._result
        if result is None or self.tree_panel.result is not result:
            return []
        paths = [
            result.path_for(frn)
            for frn in self.tree_panel.selected_frns()
            if frn != result.root_frn
        ]
        unique = list(dict.fromkeys(path for path in paths if path))
        return [
            path for path in unique
            if not any(
                path != other and os.path.normcase(path).startswith(
                    os.path.normcase(other.rstrip("\\/") + os.sep)
                )
                for other in unique
            )
        ]

    def _show_tree_menu(self, event: tk.Event) -> None:
        iid = self.tree_panel.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree_panel.tree.selection():
            self.tree_panel.tree.selection_set(iid)
        paths = self._selected_paths()
        selected = bool(paths) and not self._file_operation_running
        menu = self._tree_menu
        menu.delete(0, "end")
        menu.add_command(label="Open", command=self._open_selection, state="normal" if selected else "disabled")
        menu.add_command(label="Show in Explorer", command=self._reveal_selection, state="normal" if selected else "disabled")
        menu.add_command(label="Copy path", command=self._copy_path, state="normal" if selected else "disabled")
        menu.add_separator()
        menu.add_command(label="Copy", command=self._copy_selection, state="normal" if selected else "disabled")
        menu.add_command(label="Cut", command=self._cut_selection, state="normal" if selected else "disabled")
        menu.add_command(label="Paste", command=self._paste_selection,
                         state="normal" if self._clipboard_paths and not self._file_operation_running else "disabled")
        menu.add_command(label="Copy toâ€¦", command=self._copy_to, state="normal" if selected else "disabled")
        menu.add_command(label="Move toâ€¦", command=self._move_to, state="normal" if selected else "disabled")
        menu.add_separator()
        menu.add_command(label="Renameâ€¦", command=self._rename_selection,
                         state="normal" if len(paths) == 1 and selected else "disabled")
        menu.add_command(label="Send to Recycle Binâ€¦", command=self._recycle_selection,
                         state="normal" if selected else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_on_double_click(self, event: tk.Event) -> None:
        iid = self.tree_panel.tree.identify_row(event.y)
        result = self._result
        if not iid.startswith("frn:") or result is None:
            return
        record = result.records.get(int(iid.split(":", 1)[1]))
        if record and not record.is_directory:
            self.tree_panel.tree.selection_set(iid)
            self._open_selection()

    def _open_selection(self) -> None:
        paths = self._selected_paths()
        if paths:
            try:
                os.startfile(paths[0])
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"Could not open the item.\n\n{exc}")

    def _reveal_selection(self) -> None:
        paths = self._selected_paths()
        if paths:
            try:
                subprocess.Popen(["explorer.exe", "/select,", paths[0]])
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"Could not open Explorer.\n\n{exc}")

    def _copy_path(self) -> None:
        paths = self._selected_paths()
        if paths:
            self.clipboard_clear()
            self.clipboard_append("\n".join(paths))
            self.status_var.set(f"Copied {len(paths)} path(s) to the clipboard.")

    def _copy_selection(self, _event: object = None) -> str:
        self._set_file_clipboard("copy")
        return "break"

    def _cut_selection(self, _event: object = None) -> str:
        self._set_file_clipboard("move")
        return "break"

    def _set_file_clipboard(self, action: str) -> None:
        paths = self._selected_paths()
        if paths:
            self._clipboard_paths = paths
            self._clipboard_action = action
            self.status_var.set(f"{len(paths)} item(s) ready to {'copy' if action == 'copy' else 'move'}.")

    def _paste_selection(self, _event: object = None) -> str:
        result = self._result
        selected = self.tree_panel.selected_frns()
        if not self._clipboard_paths or result is None or len(selected) != 1:
            return "break"
        frn = selected[0]
        record = result.records.get(frn)
        if record is None:
            return "break"
        destination = result.path_for(frn if record.is_directory else record.parent_frn)
        if destination:
            self._run_file_operation(self._clipboard_action, self._clipboard_paths, destination)
        return "break"

    def _copy_to(self) -> None:
        self._choose_destination("copy")

    def _move_to(self) -> None:
        self._choose_destination("move")

    def _choose_destination(self, action: str) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        destination = filedialog.askdirectory(parent=self, title=f"{action.title()} to folder")
        if destination:
            self._run_file_operation(action, paths, destination)

    def _rename_selection(self, _event: object = None) -> str:
        paths = self._selected_paths()
        if len(paths) != 1:
            return "break"
        path = paths[0]
        name = simpledialog.askstring(
            "Rename", "New name:", initialvalue=os.path.basename(path), parent=self
        )
        if not name or name == os.path.basename(path):
            return "break"
        if name in {".", ".."} or any(char in name for char in '<>:"/\\|?*') or name.endswith((".", " ")):
            messagebox.showerror(APP_NAME, "That name is not valid for a Windows file.")
            return "break"
        self._run_file_operation("rename", paths, os.path.join(os.path.dirname(path), name))
        return "break"

    def _recycle_selection(self, _event: object = None) -> str:
        paths = self._selected_paths()
        if paths and messagebox.askyesno(
            "Send to Recycle Bin",
            f"Send {len(paths)} selected item(s) to the Recycle Bin?",
            parent=self,
        ):
            self._run_file_operation("recycle", paths)
        return "break"

    def _delete_suggestion(self, suggestion: CleanupSuggestion) -> None:
        result = self._result
        if result is None or self._file_operation_running:
            return
        if not any(s.path == suggestion.path for s in self._conversation.cleanup_suggestions()):
            messagebox.showinfo(APP_NAME, "That item is not in the current scan's cleanup list. Scan again for fresh suggestions.")
            return
        size = format_bytes(suggestion.allocated)
        if suggestion.action == "empty_recycle_bin":
            prompt = f"Empty the Recycle Bin on {result.volume.drive} ({size})?\n\nThis cannot be undone."
        else:
            target = "everything inside" if suggestion.contents_only else ""
            prompt = (
                f"Permanently delete {target}\n{suggestion.path}\n({size})?\n\n{suggestion.reason}\n\n"
                "This skips the Recycle Bin and cannot be undone. Files in use are skipped."
            )

        def operation() -> bool:
            if suggestion.action == "empty_recycle_bin":
                empty_recycle_bin(result.volume.drive)
                return True
            return delete_permanently(suggestion.path, suggestion.contents_only) == 0

        if messagebox.askyesno("Delete permanently", prompt, icon="warning", parent=self):
            self._run_file_operation("permanent delete", [suggestion.path], operation=operation)

    def _run_file_operation(
        self,
        action: str,
        paths: list[str],
        destination: str | None = None,
        operation: Callable[[], bool] | None = None,
    ) -> None:
        if self._file_operation_running or not paths:
            return
        if destination and action in {"copy", "move"}:
            target = os.path.normcase(os.path.abspath(destination))
            if any(
                os.path.isdir(source) and (
                    target == os.path.normcase(source)
                    or target.startswith(os.path.normcase(source.rstrip("\\/") + os.sep))
                )
                for source in paths
            ):
                messagebox.showerror(APP_NAME, "Choose a folder outside the selected source folder.")
                return
        result = self._result
        requires_rescan = (
            action != "copy"
            or result is None
            or os.path.normcase(os.path.splitdrive(os.path.abspath(destination or ""))[0])
            == os.path.normcase(result.volume.drive)
        )
        self._file_operation_running = True
        if requires_rescan:
            self._size_cancel.set()
        self.scan_button.configure(state="disabled")
        self.drive_box.configure(state="disabled")
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(12)
        self.progress_phase.set(f"Windows: {action}")
        self.progress_detail.set("Workingâ€¦")
        self.status_var.set(f"Windows is performing: {action}â€¦")
        generation = self._generation
        owner_hwnd = self.winfo_id()

        def worker() -> None:
            try:
                completed = operation() if operation else shell_file_operation(action, paths, destination, owner_hwnd)
                self._events.put(("operation_done", generation, action, completed, requires_rescan))
            except Exception as exc:
                self._events.put(("operation_error", generation, exc, requires_rescan))

        threading.Thread(target=worker, daemon=True, name="sage-file-operation").start()

    def _operation_finished(self, action: str, completed: bool, requires_rescan: bool) -> None:
        self._file_operation_running = False
        self.scan_button.configure(state="normal")
        self.drive_box.configure(state="readonly")
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", value=0)
        self.progress_phase.set("File operation finished")
        self.progress_detail.set("Ready")
        if action == "move":
            self._clipboard_paths = []
        outcome = "completed" if completed else (
            "finished, but some files were in use and were skipped"
            if action == "permanent delete" else "cancelled or only partly completed"
        )
        if requires_rescan:
            self._pending_size_completion = None
            self.assistant_panel.write("SAGE", f"File operation {outcome}: {action}. Refreshing the drive tree.")
            self._start_scan()
        else:
            self.assistant_panel.write("SAGE", f"File operation {outcome}: {action}.")
            if self._pending_size_completion is not None:
                self._size_finished(self._pending_size_completion)
                self._pending_size_completion = None
            elif self._result is not None and not self._result.sizes_complete:
                self.status_var.set("Copy finished â€” background size analysis continues.")

    def _close(self) -> None:
        if self._file_operation_running:
            messagebox.showinfo(APP_NAME, "Wait for the current Windows file operation to finish.")
            return
        self._scan_cancel.set()
        self._size_cancel.set()
        self.destroy()
