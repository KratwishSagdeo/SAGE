"""Evidence assistant pane."""

from __future__ import annotations

import tkinter as tk
import time
from collections.abc import Callable
from tkinter import ttk

from ..services.evidence import format_bytes
from .markdown import markdown_spans
from .styles import (
    ACCENT, CONTROL_BORDER, FONT, FONT_CODE, FONT_SEMIBOLD,
    SELECTION, SURFACE, TEXT, TEXT_SECONDARY,
)

# Cycling status messages shown while the AI is processing.
# These are illustrative progress stages — not claims about actual model internals.
_THINKING_PHASES = (
    "Thinking through your question\u2026",
    "Pondering the details\u2026",
    "Finding relevant information\u2026",
    "Analyzing the results\u2026",
    "Preparing your response\u2026",
)
_PHASE_INTERVAL_MS = 3400  # time between automatic phase transitions
_DOTS_FRAMES = ("\u2009\u2009\u2009", "\u00b7\u2009\u2009", "\u00b7\u00b7\u2009", "\u00b7\u00b7\u00b7")  # pulsing dots


class AssistantPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, on_question: Callable[[str, str], str | None]) -> None:
        super().__init__(parent, style="Surface.TFrame")
        self.on_question = on_question
        self._busy = False
        self._reply_mark: str | None = None
        self._reply_buffer = ""
        self._render_job: str | None = None
        self._spinner_job: str | None = None
        self._phase_job: str | None = None
        self._started = 0.0
        self._phase = "Ready"
        self._phase_index = 0
        self._dot_index = 0
        self._got_backend_status = False
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # ── Section header ────────────────────────────────────────────
        ttk.Label(self, text="Assistant", style="Section.TLabel", padding=(16, 12, 16, 10)).grid(
            row=0, column=0, columnspan=2, sticky="ew"
        )
        ttk.Separator(self, orient="horizontal").grid(row=1, column=0, columnspan=2, sticky="ew")

        # ── Transcript ────────────────────────────────────────────────
        self.transcript = tk.Text(
            self,
            wrap="word",
            state="disabled",
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            padx=16,
            pady=12,
            font=FONT,
            background=SURFACE,
            foreground=TEXT,
            selectbackground=SELECTION,
            selectforeground=TEXT,
            inactiveselectbackground=SELECTION,
            spacing1=2,
            spacing3=2,
            exportselection=False,
        )
        self.transcript.grid(row=2, column=0, sticky="nsew")
        self.transcript.bind("<<Selection>>", self._capture_selection)
        self.transcript.bind("<Button-3>", self._show_transcript_menu)
        self._quote = ""
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.transcript.yview)
        scrollbar.grid(row=2, column=1, sticky="ns")
        self.transcript.configure(yscrollcommand=scrollbar.set)
        self.transcript.tag_configure("speaker", font=("Segoe UI Semibold", 9), spacing3=4)
        self.transcript.tag_configure("speaker_sage", foreground=ACCENT)
        self.transcript.tag_configure("speaker_you", foreground=TEXT_SECONDARY)
        self.transcript.tag_configure("bold", font=FONT_SEMIBOLD)
        self.transcript.tag_configure("heading", font=FONT_SEMIBOLD, spacing1=10, spacing3=2)
        self.transcript.tag_configure("italic", font=("Segoe UI", 10, "italic"))
        self.transcript.tag_configure("code", font=FONT_CODE, background="#F5F5F5")

        # Context menu for transcript (read-only)
        self._transcript_menu = tk.Menu(self, tearoff=False)
        self._transcript_menu.add_command(label="Copy", command=self._copy_transcript)
        self._transcript_menu.add_command(label="Select All", command=self._select_all_transcript)

        # ── Footer: activity + quote + composer ───────────────────────
        ttk.Separator(self, orient="horizontal").grid(row=3, column=0, columnspan=2, sticky="ew")
        footer = ttk.Frame(self, style="Surface.TFrame", padding=(16, 10, 16, 14))
        footer.grid(row=4, column=0, columnspan=2, sticky="ew")
        footer.columnconfigure(0, weight=1)

        # Activity indicator
        self.activity = ttk.Frame(footer, style="Surface.TFrame")
        self.activity.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._dots_var = tk.StringVar(value="")
        self._dots_label = ttk.Label(self.activity, textvariable=self._dots_var,
                                     style="AccentMuted.TLabel", width=4)
        self._dots_label.pack(side="left")
        self.activity_text = tk.StringVar(value="Ready for your question.")
        ttk.Label(self.activity, textvariable=self.activity_text, style="SurfaceMuted.TLabel").pack(
            side="left", padx=(0, 8)
        )

        self.selected_text = tk.StringVar()
        self.selected_label = ttk.Label(footer, textvariable=self.selected_text, style="SurfaceMuted.TLabel")
        self.selected_label.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.selected_label.grid_remove()

        # Quote bar
        self.quote_bar = ttk.Frame(footer, style="Quote.TFrame", padding=(10, 5, 6, 5))
        self.quote_bar.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        self.quote_bar.columnconfigure(0, weight=1)
        self.quote_text = tk.StringVar()
        ttk.Label(self.quote_bar, textvariable=self.quote_text, style="Quote.TLabel", width=1).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(self.quote_bar, text="Clear", style="Quote.TButton", command=self.clear_quote).grid(
            row=0, column=1, padx=(8, 0)
        )
        self.quote_bar.grid_remove()

        # Composer
        composer = ttk.Frame(footer, style="Surface.TFrame")
        composer.grid(row=3, column=0, sticky="ew")
        composer.columnconfigure(0, weight=1)
        composer.rowconfigure(0, weight=1)

        self.question = tk.Text(
            composer,
            wrap="word",
            height=3,
            font=FONT,
            background=SURFACE,
            foreground=TEXT,
            borderwidth=0,
            relief="flat",
            highlightthickness=1,
            highlightcolor=ACCENT,
            highlightbackground=CONTROL_BORDER,
            padx=10,
            pady=8,
            undo=True,
            insertbackground=TEXT,
            selectbackground=SELECTION,
            selectforeground=TEXT,
        )
        self.question.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.question.bind("<Return>", self._submit)
        self.question.bind("<KP_Enter>", self._submit)
        self.question.bind("<Shift-Return>", self._newline)
        self.question.bind("<Shift-KP_Enter>", self._newline)
        self.question.bind("<Control-Return>", self._submit)
        self.question.bind("<Button-3>", self._show_prompt_menu)

        btn_frame = ttk.Frame(composer, style="Surface.TFrame")
        btn_frame.grid(row=0, column=1, sticky="ns")
        btn_frame.rowconfigure(1, weight=1)
        self.ask_button = ttk.Button(btn_frame, text="Ask", style="Accent.TButton",
                                     command=self._submit, width=6)
        self.ask_button.grid(row=0, column=0, sticky="ew")

        ttk.Label(composer, text="Enter to send   ·   Shift+Enter for a new line", style="Hint.TLabel").grid(
            row=1, column=0, sticky="w", pady=(3, 0)
        )

        # Context menu for prompt (editable)
        self._prompt_menu = tk.Menu(self, tearoff=False)
        self._prompt_menu.add_command(label="Cut", command=self._cut_prompt)
        self._prompt_menu.add_command(label="Copy", command=self._copy_prompt)
        self._prompt_menu.add_command(label="Paste", command=self._paste_prompt)
        self._prompt_menu.add_separator()
        self._prompt_menu.add_command(label="Select All", command=self._select_all_prompt)

        self.write(
            "SAGE",
            "Hey! I\u2019m SAGE. Click a folder in the list and ask about it, "
            "or name a folder, and I will answer from the scan.",
        )

    # ── Transcript helpers ────────────────────────────────────────────

    def write(self, speaker: str, message: str) -> None:
        self.transcript.configure(state="normal")
        if self.transcript.index("end-1c") != "1.0":
            self.transcript.insert("end", "\n\n")
        self.transcript.insert("end", f"{speaker}\n", ("speaker", "speaker_sage" if speaker == "SAGE" else "speaker_you"))
        for text, tags in markdown_spans(message) if speaker == "SAGE" else [(message, ())]:
            self.transcript.insert("end", text, tags)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _capture_selection(self, _event: object = None) -> None:
        ranges = self.transcript.tag_ranges("sel")
        text = self.transcript.get(ranges[0], ranges[1]).strip() if ranges else ""
        if text:
            self._quote = text[:2000]
            preview = " ".join(text.split())
            suffix = "\u2026" if len(preview) > 200 else ""
            self.quote_text.set(f"Asking about: \u201c{preview[:200]}{suffix}\u201d")
            self.quote_bar.grid()

    def set_selected_item(self, path: str | None) -> None:
        """Show the folder a click will use when the question does not name another one."""
        if not path:
            self.selected_label.grid_remove()
            return
        self.selected_text.set(f"Selected: {path}")
        self.selected_label.grid()

    def clear_quote(self) -> None:
        self._quote = ""
        self.quote_bar.grid_remove()

    def add_cleanup_actions(self, suggestions: list, on_delete: Callable) -> None:
        """List verified cleanup items, each with its own delete button."""
        if not suggestions:
            return
        self.transcript.configure(state="normal")
        for index, suggestion in enumerate(suggestions, 1):
            self.transcript.insert("end", f"\n\n{index}. ")
            self.transcript.insert("end", format_bytes(suggestion.allocated), ("bold",))
            self.transcript.insert("end", "  ")
            self.transcript.insert("end", suggestion.path, ("code",))
            self.transcript.insert("end", f"\n{suggestion.title}. {suggestion.reason}\n")
            label = "Empty Recycle Bin" if suggestion.action == "empty_recycle_bin" else (
                "Delete contents permanently" if suggestion.contents_only else "Delete permanently"
            )
            button = ttk.Button(self.transcript, text=label, style="Danger.TButton", command=lambda item=suggestion: on_delete(item))
            self.transcript.window_create("end", window=button, pady=2)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def add_open_buttons(self, links: list[dict], on_open: Callable[[str], None]) -> None:
        """Open the Windows Settings page the reply named."""
        if not links:
            return
        self.transcript.configure(state="normal")
        self.transcript.insert("end", "\n")
        for link in links:
            button = ttk.Button(
                self.transcript, text=link["label"], style="Accent.TButton",
                command=lambda target=link["target"]: on_open(target),
            )
            self.transcript.window_create("end", window=button, padx=6, pady=4)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    # ── AI response lifecycle ─────────────────────────────────────────

    def begin_reply(self) -> None:
        self.write("SAGE", "")
        self._reply_mark = "assistant_reply"
        self.transcript.mark_set(self._reply_mark, "end-1c")
        self.transcript.mark_gravity(self._reply_mark, "left")
        self.transcript.mark_set("assistant_reply_end", "end-1c")
        self.transcript.mark_gravity("assistant_reply_end", "left")
        self._reply_buffer = ""
        self._busy = True
        self._got_backend_status = False
        self._phase_index = 0
        self._dot_index = 0
        self.ask_button.configure(state="disabled")
        self._started = time.monotonic()
        self._set_thinking_phase(_THINKING_PHASES[0])
        self._animate()
        self._schedule_next_phase()

    def set_status(self, phase: str) -> None:
        """Called by the backend with a real stage update."""
        self._got_backend_status = True
        self._phase = phase
        # Don't override the cycling display text — _animate handles it

    def _set_thinking_phase(self, text: str) -> None:
        """Set the current thinking phase text (from cycling or backend)."""
        self._phase = text
        self.activity_text.set(text)

    def _schedule_next_phase(self) -> None:
        """Auto-cycle through thinking phases while busy."""
        if self._phase_job:
            self.after_cancel(self._phase_job)
        if not self._busy:
            return
        self._phase_job = self.after(_PHASE_INTERVAL_MS, self._advance_phase)

    def _advance_phase(self) -> None:
        """Move to the next thinking phase message."""
        self._phase_job = None
        if not self._busy:
            return
        self._phase_index = (self._phase_index + 1) % len(_THINKING_PHASES)
        self._set_thinking_phase(_THINKING_PHASES[self._phase_index])
        self._schedule_next_phase()

    def _animate(self) -> None:
        if not self._busy:
            return
        # Pulsing dots animation
        self._dots_var.set(_DOTS_FRAMES[self._dot_index % len(_DOTS_FRAMES)])
        self._dot_index += 1
        elapsed = int(time.monotonic() - self._started)
        # Show elapsed time alongside the current phase
        if elapsed > 0:
            self.activity_text.set(f"{self._phase}  {elapsed}s")
        else:
            self.activity_text.set(self._phase)
        self._spinner_job = self.after(300, self._animate)

    def append_reply(self, text: str) -> None:
        if self._reply_mark is None:
            return
        self._reply_buffer += text
        if self._render_job is None:
            self._render_job = self.after(80, self._render_reply)

    def _render_reply(self, final: bool = False) -> None:
        self._render_job = None
        if self._reply_mark is None:
            return
        self.transcript.configure(state="normal")
        end = self.transcript.index("assistant_reply_end")
        self.transcript.delete(self._reply_mark, end)
        self.transcript.mark_set("assistant_reply_insert", self._reply_mark)
        self.transcript.mark_gravity("assistant_reply_insert", "right")
        for text, tags in markdown_spans(self._reply_buffer.replace("\\\\", "\\"), partial=not final):
            self.transcript.insert("assistant_reply_insert", text, tags)
        self.transcript.mark_set("assistant_reply_end", "assistant_reply_insert")
        self.transcript.mark_unset("assistant_reply_insert")
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def finish_reply(self, message: str) -> None:
        if self._render_job:
            self.after_cancel(self._render_job)
            self._render_job = None
        if self._phase_job:
            self.after_cancel(self._phase_job)
            self._phase_job = None
        if self._reply_mark:
            self._reply_buffer = message
            self._render_reply(final=True)
            self.transcript.mark_unset(self._reply_mark, "assistant_reply_end")
            self._reply_mark = None
        if self._spinner_job:
            self.after_cancel(self._spinner_job)
            self._spinner_job = None
        self._busy = False
        self._dots_var.set("")
        elapsed = time.monotonic() - self._started
        self.activity_text.set(f"Answered in {elapsed:.1f}s")
        self.ask_button.configure(state="normal")

    # ── Submit ────────────────────────────────────────────────────────

    def _newline(self, _event: object = None) -> str:
        self.question.insert("insert", "\n")
        return "break"

    def _submit(self, _event: object = None) -> str | None:
        question = self.question.get("1.0", "end-1c").strip()
        if not question or self._busy:
            return "break"
        self.question.delete("1.0", "end")
        quote = self._quote
        self.clear_quote()
        if quote:
            preview = " ".join(quote.split())
            suffix = "\u2026" if len(preview) > 200 else ""
            self.write("You", f"\u201c{preview[:200]}{suffix}\u201d\n{question}")
        else:
            self.write("You", question)
        try:
            answer = self.on_question(question, quote)
        except RuntimeError as exc:
            answer = str(exc)
        if answer is not None:
            self.write("SAGE", answer)
        return "break"

    # ── Context menu handlers ─────────────────────────────────────────

    def _show_transcript_menu(self, event: tk.Event) -> None:
        has_sel = bool(self.transcript.tag_ranges("sel"))
        self._transcript_menu.entryconfigure("Copy", state="normal" if has_sel else "disabled")
        try:
            self._transcript_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._transcript_menu.grab_release()

    def _copy_transcript(self) -> None:
        try:
            text = self.transcript.get("sel.first", "sel.last")
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:
            pass

    def _select_all_transcript(self) -> None:
        self.transcript.tag_add("sel", "1.0", "end-1c")

    def _show_prompt_menu(self, event: tk.Event) -> None:
        has_sel = bool(self.question.tag_ranges("sel"))
        self._prompt_menu.entryconfigure("Cut", state="normal" if has_sel else "disabled")
        self._prompt_menu.entryconfigure("Copy", state="normal" if has_sel else "disabled")
        try:
            self._prompt_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._prompt_menu.grab_release()

    def _cut_prompt(self) -> None:
        try:
            text = self.question.get("sel.first", "sel.last")
            self.clipboard_clear()
            self.clipboard_append(text)
            self.question.delete("sel.first", "sel.last")
        except tk.TclError:
            pass

    def _copy_prompt(self) -> None:
        try:
            text = self.question.get("sel.first", "sel.last")
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:
            pass

    def _paste_prompt(self) -> None:
        try:
            text = self.clipboard_get()
            try:
                self.question.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            self.question.insert("insert", text)
        except tk.TclError:
            pass

    def _select_all_prompt(self) -> None:
        self.question.tag_add("sel", "1.0", "end-1c")
        return "break"

    def destroy(self) -> None:
        for job in (self._spinner_job, self._render_job, self._phase_job):
            if job:
                self.after_cancel(job)
        super().destroy()
