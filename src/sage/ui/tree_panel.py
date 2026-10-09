"""Lazy-loading drive tree for large MFT result sets."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..models import FileRecord, ScanResult
from ..services.evidence import attributes_text, format_bytes


class TreePanel(ttk.Frame):
    COLUMNS = ("allocated", "logical", "items", "files", "folders", "attrs")

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, style="Surface.TFrame")
        self.result: ScanResult | None = None
        self.folder_icon, self.file_icon, self.drive_icon = self._make_icons()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(self, columns=self.COLUMNS, show="tree headings")
        self.tree.heading("#0", text="Name", anchor="w")
        headings = {
            "allocated": "Allocated",
            "logical": "Size",
            "items": "Items",
            "files": "Files",
            "folders": "Folders",
            "attrs": "Attr",
        }
        for column, label in headings.items():
            self.tree.heading(column, text=label, anchor="e" if column != "attrs" else "center")

        self.tree.column("#0", width=330, minwidth=180, stretch=True)
        self.tree.column("allocated", width=100, anchor="e")
        self.tree.column("logical", width=100, anchor="e")
        self.tree.column("items", width=80, anchor="e")
        self.tree.column("files", width=80, anchor="e")
        self.tree.column("folders", width=75, anchor="e")
        self.tree.column("attrs", width=55, anchor="center")

        ybar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewOpen>>", self._on_open)

    def clear(self) -> None:
        self.result = None
        self.tree.delete(*self.tree.get_children(""))

    def selected_frns(self) -> list[int]:
        selected: list[int] = []
        for iid in self.tree.selection():
            if iid.startswith("frn:"):
                selected.append(int(iid.split(":", 1)[1]))
        return selected

    @staticmethod
    def _make_icons() -> tuple[tk.PhotoImage, tk.PhotoImage, tk.PhotoImage]:
        folder = tk.PhotoImage(width=18, height=18)
        folder.put("#E8A317", to=(2, 4, 8, 6))
        folder.put("#F2B635", to=(2, 6, 16, 15))
        folder.put("#F7CB5E", to=(2, 7, 16, 8))
        file = tk.PhotoImage(width=18, height=18)
        file.put("#9AA0A6", to=(4, 2, 14, 16))
        file.put("#FFFFFF", to=(5, 3, 13, 15))
        file.put("#C4C7CC", to=(7, 7, 11, 8))
        file.put("#C4C7CC", to=(7, 10, 11, 11))
        drive = tk.PhotoImage(width=18, height=18)
        drive.put("#5F6B7A", to=(2, 6, 16, 13))
        drive.put("#8A96A3", to=(3, 7, 15, 11))
        drive.put("#0F6CBD", to=(12, 11, 14, 12))
        return folder, file, drive

    def show_result(self, result: ScanResult) -> None:
        self.result = result
        self.tree.delete(*self.tree.get_children(""))
        root = result.records[result.root_frn]
        iid = self._iid(root.frn)
        label = result.volume.label or "Local Disk"
        self.tree.insert(
            "",
            "end",
            iid=iid,
            text=f"[{result.volume.drive}] {label}",
            image=self.drive_icon,
            values=self._values(root),
            open=True,
        )
        self._insert_children(root.frn)

    def refresh_loaded_rows(self) -> None:
        if self.result is None:
            return
        pending = list(self.tree.get_children(""))
        while pending:
            iid = pending.pop()
            if iid.startswith("dummy:"):
                continue
            try:
                frn = int(iid.split(":", 1)[1])
            except (IndexError, ValueError):
                continue
            record = self.result.records.get(frn)
            if record:
                self.tree.item(iid, values=self._values(record))
            pending.extend(self.tree.get_children(iid))

    def _insert_children(self, parent_frn: int) -> None:
        if self.result is None:
            return
        parent_iid = self._iid(parent_frn)
        child_ids = sorted(
            self.result.children.get(parent_frn, ()),
            key=lambda frn: (
                not self.result.records[frn].is_directory,
                self.result.records[frn].name.casefold(),
            ),
        )
        for child_frn in child_ids:
            child = self.result.records.get(child_frn)
            if child is None:
                continue
            child_iid = self._iid(child_frn)
            if self.tree.exists(child_iid):
                continue
            self.tree.insert(
                parent_iid,
                "end",
                iid=child_iid,
                text=child.name,
                image=self.folder_icon if child.is_directory else self.file_icon,
                values=self._values(child),
            )
            if child.is_directory and self.result.children.get(child_frn):
                self.tree.insert(child_iid, "end", iid=f"dummy:{child_frn}", text="")

    def _on_open(self, _event: object) -> None:
        iid = self.tree.focus()
        if not iid.startswith("frn:"):
            return
        children = self.tree.get_children(iid)
        for child_iid in children:
            if child_iid.startswith("dummy:"):
                self.tree.delete(child_iid)
                self._insert_children(int(iid.split(":", 1)[1]))
                break

    def _values(self, record: FileRecord) -> tuple[str, ...]:
        result = self.result
        if result is None:
            return ("—",) * len(self.COLUMNS)
        if record.is_directory:
            items, files, folders = result.stats.get(record.frn, (0, 0, 0))
            logical, allocated = result.directory_sizes.get(record.frn, (0, 0))
        else:
            items = files = folders = 0
            logical, allocated = record.logical_size, record.allocated_size

        if record.is_directory:
            if result.sizes_complete or files == 0:
                allocated_text = format_bytes(allocated)
                logical_text = format_bytes(logical)
            elif logical or allocated:
                allocated_text = format_bytes(allocated) + "+"
                logical_text = format_bytes(logical) + "+"
            else:
                allocated_text = logical_text = "Pending…"
        elif record.size_error:
            allocated_text = logical_text = "Unavailable"
        elif record.size_ready:
            allocated_text = format_bytes(allocated)
            logical_text = format_bytes(logical)
        else:
            allocated_text = logical_text = "Pending…"
        return (
            allocated_text,
            logical_text,
            f"{items:,}" if record.is_directory else "",
            f"{files:,}" if record.is_directory else "",
            f"{folders:,}" if record.is_directory else "",
            attributes_text(record.attributes),
        )

    @staticmethod
    def _iid(frn: int) -> str:
        return f"frn:{frn}"
