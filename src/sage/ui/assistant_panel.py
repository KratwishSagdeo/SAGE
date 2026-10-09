"""Evidence assistant pane."""

from __future__ import annotations

import tkinter as tk
import time
from collections.abc import Callable
from tkinter import ttk

from ..services.evidence import format_bytes
from .markdown import markdown_spans
from .styles import ACCENT, FONT, FONT_CODE, FONT_SEMIBOLD, SELECTION, SURFACE, TEXT, TEXT_SECONDARY


class AssistantPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, on_question: Callable[[str, str], str | None]) -> None:
        super().__init__(parent, style="Surface.TFrame")
        self.on_question = on_question
        self._busy = False
        self._reply_mark: str | None = None
        self._reply_buffer = ""
        self._render_job: str | None = None
        self._spinner_job: str | None = None
        self._started = 0.0
        self._phase = "Ready"
        self._spin_index = 0
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        ttk.Label(self, text="Assistant", style="Section.TLabel", padding=(16, 12, 16, 10)).grid(
            row=0, column=0, columnspan=2, sticky="ew"
        )
        ttk.Separator(self, orient="horizontal").grid(row=1, column=0, columnspan=2, sticky="ew")
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
            spacing1=1,
            spacing3=1,
            exportselection=False,
        )
        self.transcript.grid(row=2, column=0, sticky="nsew")
        self.transcript.bind("<<Selection>>", self._capture_selection)
        self._quote = ""
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.transcript.yview)
        scrollbar.grid(row=2, column=1, sticky="ns")
        self.transcript.configure(yscrollcommand=scrollbar.set)
        self.transcript.tag_configure("speaker", font=("Segoe UI Semibold", 9), spacing3=3)
        self.transcript.tag_configure("speaker_sage", foreground=ACCENT)
        self.transcript.tag_configure("speaker_you", foreground=TEXT_SECONDARY)
        self.transcript.tag_configure("bold", font=FONT_SEMIBOLD)
        self.transcript.tag_configure("italic", font=("Segoe UI", 10, "italic"))
        self.transcript.tag_configure("code", font=FONT_CODE, background="#F5F5F5")

        ttk.Separator(self, orient="horizontal").grid(row=3, column=0, columnspan=2, sticky="ew")
        footer = ttk.Frame(self, style="Surface.TFrame", padding=(16, 8, 16, 14))
        footer.grid(row=4, column=0, columnspan=2, sticky="ew")
        footer.columnconfigure(0, weight=1)

        self.activity = ttk.Frame(footer, style="Surface.TFrame")
        self.activity.grid(row=0, column=0, sticky="ew")
        self.spinner = ttk.Label(self.activity, text="", width=2, style="SurfaceMuted.TLabel")
        self.spinner.pack(side="left")
        self.activity_text = tk.StringVar(value="Ready")
        ttk.Label(self.activity, textvariable=self.activity_text, style="SurfaceMuted.TLabel").pack(side="left")

        self.quote_bar = ttk.Frame(footer, style="Quote.TFrame", padding=(10, 4, 4, 4))
        self.quote_bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.quote_bar.columnconfigure(0, weight=1)
        self.quote_text = tk.StringVar()
        # width=1 stops a long selection from claiming the button's space.
        ttk.Label(self.quote_bar, textvariable=self.quote_text, style="Quote.TLabel", width=1).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(self.quote_bar, text="Clear", style="Quote.TButton", command=self.clear_quote).grid(
            row=0, column=1, padx=(8, 0)
        )
        self.quote_bar.grid_remove()

        composer = ttk.Frame(footer, style="Surface.TFrame")
        composer.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        composer.columnconfigure(0, weight=1)
        self.question = ttk.Entry(composer)
        self.question.grid(row=0, column=0, sticky="ew", padx=(0, 8), ipady=2)
        self.question.bind("<Return>", self._submit)
        self.ask_button = ttk.Button(composer, text="Ask", style="Accent.TButton", command=self._submit)
        self.ask_button.grid(row=0, column=1)

        self.write(
            "SAGE",
            "Hey! I'm SAGE. Scan a drive and ask me about a folder by name, "
            "what's using space, or what to consider before removing something.",
        )

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
            self.quote_text.set(f"Asking about: “{preview[:200]}{'…' if len(preview) > 200 else ''}”")
            self.quote_bar.grid()

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

    def begin_reply(self) -> None:
        self.write("SAGE", "")
        self._reply_mark = "assistant_reply"
        self.transcript.mark_set(self._reply_mark, "end-1c")
        self.transcript.mark_gravity(self._reply_mark, "left")
        self.transcript.mark_set("assistant_reply_end", "end-1c")
        self.transcript.mark_gravity("assistant_reply_end", "left")
        self._reply_buffer = ""
        self._busy = True
        self.ask_button.configure(state="disabled")
        self._started = time.monotonic()
        self.set_status("Preparing your answer…")
        self._animate()

    def set_status(self, phase: str) -> None:
        self._phase = phase
        self.activity_text.set(phase)

    def _animate(self) -> None:
        if not self._busy:
            return
        self.spinner.configure(text=("◐", "◓", "◑", "◒")[self._spin_index % 4])
        self._spin_index += 1
        elapsed = int(time.monotonic() - self._started)
        self.activity_text.set(f"{self._phase}  {elapsed}s")
        self._spinner_job = self.after(120, self._animate)

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
        if self._reply_mark:
            self._reply_buffer = message
            self._render_reply(final=True)
            self.transcript.mark_unset(self._reply_mark, "assistant_reply_end")
            self._reply_mark = None
        if self._spinner_job:
            self.after_cancel(self._spinner_job)
            self._spinner_job = None
        self._busy = False
        self.spinner.configure(text="")
        elapsed = time.monotonic() - self._started
        self.activity_text.set(f"Answered in {elapsed:.1f}s")
        self.ask_button.configure(state="normal")

    def _submit(self, _event: object = None) -> None:
        question = self.question.get().strip()
        if not question or self._busy:
            return
        self.question.delete(0, "end")
        quote = self._quote
        self.clear_quote()
        if quote:
            preview = " ".join(quote.split())
            self.write("You", f"“{preview[:200]}{'…' if len(preview) > 200 else ''}”\n{question}")
        else:
            self.write("You", question)
        try:
            answer = self.on_question(question, quote)
        except RuntimeError as exc:
            answer = str(exc)
        if answer is not None:
            self.write("SAGE", answer)

    def destroy(self) -> None:
        for job in (self._spinner_job, self._render_job):
            if job:
                self.after_cancel(job)
        super().destroy()
