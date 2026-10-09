"""Office-style ttk theme: light neutral surfaces, one blue accent, Segoe UI."""

from __future__ import annotations

from tkinter import ttk

BG = "#F3F3F3"
SURFACE = "#FFFFFF"
BORDER = "#E0E0E0"
CONTROL_BORDER = "#C8C8C8"
TEXT = "#1F1F1F"
TEXT_SECONDARY = "#616161"
ACCENT = "#0F6CBD"
ACCENT_HOVER = "#115EA3"
ACCENT_PRESSED = "#0F548C"
SELECTION = "#CCE4F7"
HOVER = "#F5F5F5"
DANGER = "#C42B1C"
QUOTE_BG = "#EBF3FC"
FONT = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_SEMIBOLD = ("Segoe UI Semibold", 10)
FONT_SECTION = ("Segoe UI Semibold", 11)
FONT_APP = ("Segoe UI Semibold", 14)
FONT_CODE = ("Consolas", 10)


def configure_styles(style: ttk.Style) -> None:
    style.theme_use("clam")
    root = style.master
    root.configure(background=BG)
    root.option_add("*Font", FONT)
    root.option_add("*Menu.background", SURFACE)
    root.option_add("*Menu.foreground", TEXT)
    root.option_add("*Menu.activeBackground", SELECTION)
    root.option_add("*Menu.activeForeground", TEXT)
    root.option_add("*Menu.borderWidth", 1)
    root.option_add("*Menu.relief", "solid")
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", SELECTION)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.font", FONT)

    style.configure(
        ".", background=BG, foreground=TEXT, font=FONT, bordercolor=BORDER,
        lightcolor=BG, darkcolor=BG, troughcolor=BG, focuscolor=ACCENT,
        selectbackground=SELECTION, selectforeground=TEXT,
    )
    style.configure("TFrame", background=BG)
    style.configure("Header.TFrame", background=SURFACE)
    style.configure("Card.TFrame", background=SURFACE, borderwidth=1, relief="solid",
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.configure("Surface.TFrame", background=SURFACE)
    style.configure("Quote.TFrame", background=QUOTE_BG)
    style.configure("TSeparator", background=BORDER)

    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("App.TLabel", background=SURFACE, font=FONT_APP)
    style.configure("Subtitle.TLabel", background=SURFACE, foreground=TEXT_SECONDARY)
    style.configure("HeaderField.TLabel", background=SURFACE, foreground=TEXT_SECONDARY)
    style.configure("Section.TLabel", background=SURFACE, font=FONT_SECTION)
    style.configure("Muted.TLabel", background=BG, foreground=TEXT_SECONDARY, font=FONT_SMALL)
    style.configure("SurfaceMuted.TLabel", background=SURFACE, foreground=TEXT_SECONDARY, font=FONT_SMALL)
    style.configure("Quote.TLabel", background=QUOTE_BG, foreground=TEXT, font=FONT_SMALL)
    style.configure("Status.TLabel", background=BG, foreground=TEXT_SECONDARY, font=FONT_SMALL, padding=(2, 4))
    style.configure("HeaderInfo.TLabel", background=SURFACE, foreground=TEXT_SECONDARY, font=FONT_SMALL)
    style.configure("Hint.TLabel", background=SURFACE, foreground=TEXT_SECONDARY, font=("Segoe UI", 8))
    style.configure("AccentMuted.TLabel", background=SURFACE, foreground=ACCENT, font=("Segoe UI", 9))

    style.configure("TButton", background=SURFACE, foreground=TEXT, bordercolor=CONTROL_BORDER,
                    lightcolor=SURFACE, darkcolor=SURFACE, padding=(12, 5), focusthickness=0)
    style.map("TButton",
              background=[("disabled", BG), ("pressed", "#E6E6E6"), ("active", HOVER)],
              foreground=[("disabled", "#A0A0A0")],
              lightcolor=[("pressed", "#E6E6E6"), ("active", HOVER)],
              darkcolor=[("pressed", "#E6E6E6"), ("active", HOVER)])
    style.configure("Accent.TButton", background=ACCENT, foreground=SURFACE, bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, font=FONT_SEMIBOLD)
    style.map("Accent.TButton",
              background=[("disabled", "#C7C7C7"), ("pressed", ACCENT_PRESSED), ("active", ACCENT_HOVER)],
              foreground=[("disabled", SURFACE)],
              bordercolor=[("disabled", "#C7C7C7"), ("pressed", ACCENT_PRESSED), ("active", ACCENT_HOVER)],
              lightcolor=[("disabled", "#C7C7C7"), ("pressed", ACCENT_PRESSED), ("active", ACCENT_HOVER)],
              darkcolor=[("disabled", "#C7C7C7"), ("pressed", ACCENT_PRESSED), ("active", ACCENT_HOVER)])
    style.configure("Danger.TButton", foreground=DANGER, padding=(10, 3), font=FONT_SMALL)
    style.configure("Quote.TButton", background=QUOTE_BG, foreground=ACCENT, bordercolor=QUOTE_BG,
                    lightcolor=QUOTE_BG, darkcolor=QUOTE_BG, padding=(8, 2), font=FONT_SMALL)
    style.map("Quote.TButton", background=[("pressed", SELECTION), ("active", "#DCEAF9")],
              lightcolor=[("active", "#DCEAF9")], darkcolor=[("active", "#DCEAF9")],
              bordercolor=[("active", "#DCEAF9")])

    for widget in ("TEntry", "TCombobox"):
        style.configure(widget, fieldbackground=SURFACE, foreground=TEXT, bordercolor=CONTROL_BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, padding=(8, 5), arrowcolor=TEXT_SECONDARY,
                        insertcolor=TEXT)
        style.map(widget, bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
    style.map("TCombobox", fieldbackground=[("readonly", SURFACE)], background=[("readonly", SURFACE)],
              selectbackground=[("readonly", SURFACE)], selectforeground=[("readonly", TEXT)])

    style.configure("Horizontal.TProgressbar", thickness=4, background=ACCENT, troughcolor="#E1E1E1",
                    bordercolor="#E1E1E1", lightcolor=ACCENT, darkcolor=ACCENT)

    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
                    rowheight=26, font=FONT, borderwidth=0)
    style.map("Treeview", background=[("selected", SELECTION)], foreground=[("selected", TEXT)])
    style.configure("Treeview.Heading", background="#FAFAFA", foreground=TEXT_SECONDARY,
                    font=("Segoe UI Semibold", 9), relief="flat", padding=(8, 5),
                    bordercolor=BORDER, lightcolor="#FAFAFA", darkcolor=BORDER)
    style.map("Treeview.Heading", background=[("active", "#F0F0F0")])

    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background="#D4D4D4", troughcolor=SURFACE,
                        bordercolor=SURFACE, lightcolor="#D4D4D4", darkcolor="#D4D4D4",
                        arrowcolor=TEXT_SECONDARY, arrowsize=12, gripcount=0, relief="flat")
        style.map(f"{orient}.TScrollbar", background=[("active", "#B8B8B8")])

    style.configure("TPanedwindow", background=BG)
    style.configure("Sash", sashthickness=10, background=BG, lightcolor=BG, bordercolor=BG)
