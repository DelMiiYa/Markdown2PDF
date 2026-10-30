"""
MD-to-PDF Desktop Studio
Standalone Python GUI application with a split-pane Markdown editor and
pixel-accurate A4 print-styled PDF preview.
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional
import urllib.parse

from PIL import Image, ImageTk

from converter import (
    MARGIN_PRESETS,
    ConversionOptions,
    ConversionResult,
    render_pdf,
    render_pdf_pages_to_pil,
)


def _enable_windows_hidpi() -> None:
    """Enable High-DPI scaling on Windows so text and preview pages are sharp."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


class MdToPdfStudioApp:
    """Main Tkinter desktop application class for MD-to-PDF Studio."""

    def __init__(self, root: tk.Tk, initial_file: Optional[str] = None) -> None:
        self.root = root
        self.root.title("MD-to-PDF Desktop Studio")
        self.root.geometry("1380x860")
        self.root.minsize(980, 620)

        # Workspace & document state
        self.current_file: Optional[Path] = None
        self.base_dir: Path = Path.cwd()
        self.is_modified: bool = False

        # Temp directory for live preview PDFs
        self._temp_dir = Path(tempfile.mkdtemp(prefix="mdtopdf_studio_"))
        self._preview_pdf_path = self._temp_dir / "_live_preview.pdf"
        self._last_rendered_result: Optional[ConversionResult] = None

        # Preview image state
        self._raw_pil_pages: list[Image.Image] = []
        self._tk_page_images: list[ImageTk.PhotoImage] = []
        self._page_y_coords: list[int] = []
        self._zoom_level: float = 1.0

        # Threading / debounce state
        self._render_queue: queue.Queue = queue.Queue()
        self._render_generation: int = 0
        self._is_rendering: bool = False
        self._debounce_after_id: Optional[str] = None
        self._syntax_after_id: Optional[str] = None

        # Tk variables for layout & styling controls
        self.var_font_size = tk.DoubleVar(value=9.5)
        self.var_margin_preset = tk.StringVar(value="Standard (12×14mm)")
        self.var_img_width = tk.IntVar(value=90)
        self.var_img_height = tk.IntVar(value=70)
        self.var_suppress_missing = tk.BooleanVar(value=True)
        self.var_keep_blocks = tk.BooleanVar(value=True)
        self.var_auto_preview = tk.BooleanVar(value=True)
        self.var_status = tk.StringVar(
            value="Ready — Open a .md file or start typing in the editor."
        )
        self.var_base_dir_label = tk.StringVar(value=f"Base Dir: {self.base_dir}")
        self.var_page_badge = tk.StringVar(value="No preview rendered")
        self.var_zoom_label = tk.StringVar(value="100%")

        self._configure_styles()
        self._build_menu()
        self._build_ui()
        self._bind_shortcuts()

        # Poll background render queue
        self.root.after(100, self._poll_render_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Only load a file if explicitly passed via CLI
        if initial_file and os.path.isfile(initial_file):
            self.load_markdown_file(Path(initial_file))
        else:
            self._draw_empty_preview_placeholder()

    # ------------------------------------------------------------------
    # UI Construction & Styling
    # ------------------------------------------------------------------

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("Toolbar.TFrame", background="#f3f4f6")
        style.configure("Settings.TFrame", background="#e5e7eb")
        style.configure("PaneHeader.TFrame", background="#1e293b")
        style.configure(
            "PaneHeader.TLabel",
            background="#1e293b",
            foreground="#f8fafc",
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Badge.TLabel",
            background="#1e293b",
            foreground="#93c5fd",
            font=("Segoe UI", 9),
        )
        style.configure("Toolbar.TButton", font=("Segoe UI", 9), padding=(8, 4))
        style.configure(
            "Primary.TButton", font=("Segoe UI", 9, "bold"), padding=(10, 4)
        )
        style.configure(
            "Settings.TLabel",
            background="#e5e7eb",
            foreground="#1f2937",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Settings.TCheckbutton",
            background="#e5e7eb",
            font=("Segoe UI", 9),
        )

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(
            label="New Markdown", accelerator="Ctrl+N", command=self.new_document
        )
        file_menu.add_command(
            label="Open Markdown (.md)...",
            accelerator="Ctrl+O",
            command=self.open_file_dialog,
        )
        file_menu.add_command(
            label="Save Markdown", accelerator="Ctrl+S", command=self.save_file
        )
        file_menu.add_command(
            label="Save Markdown As...",
            accelerator="Ctrl+Shift+S",
            command=self.save_file_as,
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="Set Image Base Folder...", command=self.choose_base_dir
        )
        file_menu.add_command(
            label="Insert Image...", accelerator="Ctrl+I", command=self.insert_image_dialog
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="Export PDF...", accelerator="Ctrl+E", command=self.export_pdf_dialog
        )
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(
            label="Refresh Print Preview",
            accelerator="F5",
            command=self.trigger_preview_render,
        )
        view_menu.add_separator()
        view_menu.add_command(
            label="Zoom In", accelerator="Ctrl++", command=lambda: self.adjust_zoom(0.15)
        )
        view_menu.add_command(
            label="Zoom Out", accelerator="Ctrl+-", command=lambda: self.adjust_zoom(-0.15)
        )
        view_menu.add_command(
            label="Reset Zoom (100%)",
            accelerator="Ctrl+0",
            command=lambda: self.set_zoom(1.0),
        )
        view_menu.add_command(
            label="Fit to Pane Width", command=self.fit_preview_to_width
        )
        menubar.add_cascade(label="View", menu=view_menu)

        self.root.config(menu=menubar)

    def _build_ui(self) -> None:
        # 1. Top Action Toolbar
        toolbar = ttk.Frame(self.root, style="Toolbar.TFrame", padding=(8, 6))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(
            toolbar, text="New", style="Toolbar.TButton", command=self.new_document
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            toolbar,
            text="Open .md...",
            style="Toolbar.TButton",
            command=self.open_file_dialog,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            toolbar, text="Save .md", style="Toolbar.TButton", command=self.save_file
        ).pack(side=tk.LEFT, padx=4)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8, pady=2
        )

        ttk.Button(
            toolbar,
            text="Insert Image...",
            style="Toolbar.TButton",
            command=self.insert_image_dialog,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            toolbar,
            text="Base Folder...",
            style="Toolbar.TButton",
            command=self.choose_base_dir,
        ).pack(side=tk.LEFT, padx=4)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8, pady=2
        )

        ttk.Button(
            toolbar,
            text="Refresh Preview (F5)",
            style="Toolbar.TButton",
            command=self.trigger_preview_render,
        ).pack(side=tk.LEFT, padx=4)

        # Right side of top toolbar: Export PDF button
        self.btn_export = tk.Button(
            toolbar,
            text="Export PDF...",
            bg="#0d47a1",
            fg="#ffffff",
            activebackground="#1565c0",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            font=("Segoe UI", 9, "bold"),
            padx=14,
            pady=4,
            cursor="hand2",
            command=self.export_pdf_dialog,
        )
        self.btn_export.pack(side=tk.RIGHT, padx=4)

        # 2. Layout & Styling Controls Bar
        settings_bar = ttk.Frame(self.root, style="Settings.TFrame", padding=(8, 5))
        settings_bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(settings_bar, text="Font (pt):", style="Settings.TLabel").pack(
            side=tk.LEFT, padx=(0, 3)
        )
        font_spin = ttk.Spinbox(
            settings_bar,
            from_=7.5,
            to=14.0,
            increment=0.5,
            textvariable=self.var_font_size,
            width=5,
            command=self._on_option_changed,
        )
        font_spin.pack(side=tk.LEFT, padx=(0, 10))
        font_spin.bind("<Return>", lambda _: self._on_option_changed())

        ttk.Label(settings_bar, text="Margins:", style="Settings.TLabel").pack(
            side=tk.LEFT, padx=(0, 3)
        )
        margin_cb = ttk.Combobox(
            settings_bar,
            textvariable=self.var_margin_preset,
            values=list(MARGIN_PRESETS.keys()),
            state="readonly",
            width=18,
        )
        margin_cb.pack(side=tk.LEFT, padx=(0, 10))
        margin_cb.bind("<<ComboboxSelected>>", lambda _: self._on_option_changed())

        ttk.Label(
            settings_bar, text="Table Img Max (W×H px):", style="Settings.TLabel"
        ).pack(side=tk.LEFT, padx=(0, 3))
        w_spin = ttk.Spinbox(
            settings_bar,
            from_=30,
            to=400,
            increment=10,
            textvariable=self.var_img_width,
            width=5,
            command=self._on_option_changed,
        )
        w_spin.pack(side=tk.LEFT)
        w_spin.bind("<Return>", lambda _: self._on_option_changed())

        ttk.Label(settings_bar, text="×", style="Settings.TLabel").pack(
            side=tk.LEFT, padx=2
        )
        h_spin = ttk.Spinbox(
            settings_bar,
            from_=30,
            to=400,
            increment=10,
            textvariable=self.var_img_height,
            width=5,
            command=self._on_option_changed,
        )
        h_spin.pack(side=tk.LEFT, padx=(0, 12))
        h_spin.bind("<Return>", lambda _: self._on_option_changed())

        ttk.Checkbutton(
            settings_bar,
            text="Suppress Missing Images",
            variable=self.var_suppress_missing,
            style="Settings.TCheckbutton",
            command=self._on_option_changed,
        ).pack(side=tk.LEFT, padx=6)

        ttk.Checkbutton(
            settings_bar,
            text="Keep Q&A / Rows Together",
            variable=self.var_keep_blocks,
            style="Settings.TCheckbutton",
            command=self._on_option_changed,
        ).pack(side=tk.LEFT, padx=6)

        ttk.Checkbutton(
            settings_bar,
            text="Auto-Refresh Preview",
            variable=self.var_auto_preview,
            style="Settings.TCheckbutton",
        ).pack(side=tk.RIGHT, padx=4)

        # 3. Main Split-Pane Workspace
        self.paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # --- Left Pane: Markdown Editor ---
        left_frame = ttk.Frame(self.paned)
        self.paned.add(left_frame, weight=1)

        left_header = ttk.Frame(left_frame, style="PaneHeader.TFrame", padding=(10, 5))
        left_header.pack(side=tk.TOP, fill=tk.X)

        self.lbl_editor_title = ttk.Label(
            left_header, text="Markdown Editor — Untitled.md", style="PaneHeader.TLabel"
        )
        self.lbl_editor_title.pack(side=tk.LEFT)

        ttk.Label(
            left_header, textvariable=self.var_base_dir_label, style="Badge.TLabel"
        ).pack(side=tk.RIGHT)

        editor_container = ttk.Frame(left_frame)
        editor_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.editor = tk.Text(
            editor_container,
            wrap=tk.WORD,
            undo=True,
            font=("Leelawadee UI", 10),
            bg="#ffffff",
            fg="#1e293b",
            insertbackground="#0d47a1",
            selectbackground="#bfdbfe",
            padx=12,
            pady=10,
            relief=tk.FLAT,
            spacing1=2,
            spacing3=2,
        )
        editor_vscroll = ttk.Scrollbar(
            editor_container, orient=tk.VERTICAL, command=self.editor.yview
        )
        self.editor.configure(yscrollcommand=editor_vscroll.set)

        editor_vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.editor.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Configure Markdown syntax highlight tags
        self.editor.tag_configure(
            "md_h1", foreground="#0d47a1", font=("Leelawadee UI", 12, "bold")
        )
        self.editor.tag_configure(
            "md_h2", foreground="#1565c0", font=("Leelawadee UI", 11, "bold")
        )
        self.editor.tag_configure(
            "md_h3", foreground="#1d4ed8", font=("Leelawadee UI", 10, "bold")
        )
        self.editor.tag_configure(
            "md_bold", foreground="#0f172a", font=("Leelawadee UI", 10, "bold")
        )
        self.editor.tag_configure("md_img", foreground="#047857", background="#ecfdf5")
        self.editor.tag_configure("md_code", foreground="#b91c1c", background="#fef2f2")
        self.editor.tag_configure("md_table", foreground="#4338ca")

        self.editor.bind("<<Modified>>", self._on_editor_modified)

        # --- Right Pane: Print-Styled A4 Preview ---
        right_frame = ttk.Frame(self.paned)
        self.paned.add(right_frame, weight=1)

        right_header = ttk.Frame(right_frame, style="PaneHeader.TFrame", padding=(10, 4))
        right_header.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(
            right_header, text="Print-Styled A4 Preview", style="PaneHeader.TLabel"
        ).pack(side=tk.LEFT)

        # Zoom & Page navigation controls in right header
        nav_frame = tk.Frame(right_header, bg="#1e293b")
        nav_frame.pack(side=tk.RIGHT)

        for label, cmd in [
            ("◀ Prev", lambda: self.scroll_to_page_delta(-1)),
            ("Next ▶", lambda: self.scroll_to_page_delta(1)),
            ("−", lambda: self.adjust_zoom(-0.15)),
        ]:
            tk.Button(
                nav_frame,
                text=label,
                bg="#334155",
                fg="#f8fafc",
                activebackground="#475569",
                activeforeground="#ffffff",
                relief=tk.FLAT,
                font=("Segoe UI", 8),
                padx=6,
                pady=1,
                command=cmd,
            ).pack(side=tk.LEFT, padx=2)

        tk.Label(
            nav_frame,
            textvariable=self.var_zoom_label,
            bg="#1e293b",
            fg="#e2e8f0",
            font=("Segoe UI", 8, "bold"),
            width=5,
        ).pack(side=tk.LEFT, padx=2)

        for label, cmd in [
            ("+", lambda: self.adjust_zoom(0.15)),
            ("Fit Width", self.fit_preview_to_width),
        ]:
            tk.Button(
                nav_frame,
                text=label,
                bg="#334155",
                fg="#f8fafc",
                activebackground="#475569",
                activeforeground="#ffffff",
                relief=tk.FLAT,
                font=("Segoe UI", 8),
                padx=6,
                pady=1,
                command=cmd,
            ).pack(side=tk.LEFT, padx=2)

        ttk.Label(
            right_header, textvariable=self.var_page_badge, style="Badge.TLabel"
        ).pack(side=tk.RIGHT, padx=12)

        preview_container = ttk.Frame(right_frame)
        preview_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.preview_canvas = tk.Canvas(
            preview_container,
            bg="#334155",
            highlightthickness=0,
        )
        preview_vscroll = ttk.Scrollbar(
            preview_container, orient=tk.VERTICAL, command=self.preview_canvas.yview
        )
        preview_hscroll = ttk.Scrollbar(
            preview_container, orient=tk.HORIZONTAL, command=self.preview_canvas.xview
        )
        self.preview_canvas.configure(
            yscrollcommand=preview_vscroll.set, xscrollcommand=preview_hscroll.set
        )

        preview_vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        preview_hscroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.preview_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Mousewheel scrolling on preview canvas (Windows & Linux)
        self.preview_canvas.bind("<Configure>", lambda _: self._redraw_preview_canvas())
        self.preview_canvas.bind("<MouseWheel>", self._on_preview_mousewheel)
        self.preview_canvas.bind(
            "<Button-4>", lambda e: self._on_linux_scroll(e, direction=-1)
        )
        self.preview_canvas.bind(
            "<Button-5>", lambda e: self._on_linux_scroll(e, direction=1)
        )

        # 4. Bottom Status Bar
        status_bar = tk.Frame(self.root, bg="#0f172a", padx=10, pady=4)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        tk.Label(
            status_bar,
            textvariable=self.var_status,
            bg="#0f172a",
            fg="#e2e8f0",
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-n>", lambda _: self.new_document())
        self.root.bind("<Control-o>", lambda _: self.open_file_dialog())
        self.root.bind("<Control-s>", lambda _: self.save_file())
        self.root.bind("<Control-S>", lambda _: self.save_file_as())
        self.root.bind("<Control-i>", lambda _: self.insert_image_dialog())
        self.root.bind("<Control-e>", lambda _: self.export_pdf_dialog())
        self.root.bind("<F5>", lambda _: self.trigger_preview_render())
        self.root.bind("<Control-plus>", lambda _: self.adjust_zoom(0.15))
        self.root.bind("<Control-equal>", lambda _: self.adjust_zoom(0.15))
        self.root.bind("<Control-minus>", lambda _: self.adjust_zoom(-0.15))
        self.root.bind("<Control-0>", lambda _: self.set_zoom(1.0))

    # ------------------------------------------------------------------
    # Document & File Operations
    # ------------------------------------------------------------------

    def _confirm_discard_changes(self) -> bool:
        if not self.is_modified:
            return True
        ans = messagebox.askyesnocancel(
            "Unsaved Changes",
            "Save changes to the current Markdown document before continuing?",
            parent=self.root,
        )
        if ans is None:
            return False
        if ans is True:
            return self.save_file()
        return True

    def new_document(self) -> None:
        if not self._confirm_discard_changes():
            return
        self.current_file = None
        self.is_modified = False
        self.editor.delete("1.0", tk.END)
        self.editor.edit_modified(False)
        self._update_header_labels()
        self._raw_pil_pages.clear()
        self._tk_page_images.clear()
        self.var_page_badge.set("No preview rendered")
        self.var_status.set("New document created.")
        self._draw_empty_preview_placeholder()

    def open_file_dialog(self) -> None:
        if not self._confirm_discard_changes():
            return
        initial_dir = str(self.base_dir)
        file_path = filedialog.askopenfilename(
            parent=self.root,
            title="Open Markdown File",
            initialdir=initial_dir,
            filetypes=[
                ("Markdown Files", "*.md *.markdown *.mdown"),
                ("Text Files", "*.txt"),
                ("All Files", "*.*"),
            ],
        )
        if file_path:
            self.load_markdown_file(Path(file_path))

    def load_markdown_file(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            text = resolved.read_text(encoding="utf-8")
        except Exception as exc:
            messagebox.showerror(
                "Error Opening File",
                f"Failed to read {path}:\n{exc}",
                parent=self.root,
            )
            return

        self.current_file = resolved
        self.base_dir = resolved.parent
        self.is_modified = False

        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", text)
        self.editor.edit_modified(False)
        self._apply_syntax_highlighting()
        self._update_header_labels()
        self.var_status.set(f"Loaded {resolved.name} (Base folder: {self.base_dir})")
        self.trigger_preview_render()

    def save_file(self) -> bool:
        if self.current_file is None:
            return self.save_file_as()
        try:
            content = self.editor.get("1.0", "end-1c")
            self.current_file.write_text(content, encoding="utf-8")
            self.is_modified = False
            self.editor.edit_modified(False)
            self._update_header_labels()
            self.var_status.set(f"Saved {self.current_file}")
            return True
        except Exception as exc:
            messagebox.showerror(
                "Save Error",
                f"Could not save file:\n{exc}",
                parent=self.root,
            )
            return False

    def save_file_as(self) -> bool:
        initial_name = self.current_file.name if self.current_file else "document.md"
        file_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save Markdown As",
            initialdir=str(self.base_dir),
            initialfile=initial_name,
            defaultextension=".md",
            filetypes=[("Markdown Files", "*.md"), ("All Files", "*.*")],
        )
        if not file_path:
            return False
        self.current_file = Path(file_path).resolve()
        self.base_dir = self.current_file.parent
        return self.save_file()

    def choose_base_dir(self) -> None:
        folder = filedialog.askdirectory(
            parent=self.root,
            title="Select Base Folder for Relative Images",
            initialdir=str(self.base_dir),
        )
        if folder:
            self.base_dir = Path(folder).resolve()
            self._update_header_labels()
            self.var_status.set(f"Image base folder set to: {self.base_dir}")
            self.trigger_preview_render()

    def insert_image_dialog(self) -> None:
        img_path = filedialog.askopenfilename(
            parent=self.root,
            title="Select Image to Insert",
            initialdir=str(self.base_dir),
            filetypes=[
                ("Image Files", "*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg"),
                ("All Files", "*.*"),
            ],
        )
        if not img_path:
            return

        chosen = Path(img_path).resolve()
        try:
            rel = chosen.relative_to(self.base_dir).as_posix()
            # URL-encode spaces and parentheses while keeping forward slashes and µ readable
            parts = [urllib.parse.quote(p, safe="µ") for p in rel.split("/")]
            md_ref = "/".join(parts)
        except ValueError:
            md_ref = chosen.as_uri()

        alt_text = chosen.stem
        tag = f"![{alt_text}]({md_ref})"
        self.editor.insert(tk.INSERT, tag)
        self.editor.focus_set()

    def _build_conversion_options(self) -> ConversionOptions:
        try:
            font_pt = float(self.var_font_size.get())
        except (tk.TclError, ValueError):
            font_pt = 9.5

        try:
            img_w = max(20, int(self.var_img_width.get()))
        except (tk.TclError, ValueError):
            img_w = 90

        try:
            img_h = max(20, int(self.var_img_height.get()))
        except (tk.TclError, ValueError):
            img_h = 70

        margin_label = self.var_margin_preset.get()
        margin_css = MARGIN_PRESETS.get(margin_label, "12mm 14mm")
        doc_title = self.current_file.stem if self.current_file else "Markdown Document"

        return ConversionOptions(
            font_size_pt=font_pt,
            margin_css=margin_css,
            img_max_width=img_w,
            img_max_height=img_h,
            suppress_missing_images=bool(self.var_suppress_missing.get()),
            keep_blocks_together=bool(self.var_keep_blocks.get()),
            title=doc_title,
        )

    def export_pdf_dialog(self) -> None:
        md_text = self.editor.get("1.0", "end-1c")
        if not md_text.strip():
            messagebox.showwarning(
                "Empty Document",
                "There is no Markdown content to export.",
                parent=self.root,
            )
            return

        default_pdf_name = (
            f"{self.current_file.stem}.pdf" if self.current_file else "output.pdf"
        )
        out_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export Print-Ready PDF",
            initialdir=str(self.base_dir),
            initialfile=default_pdf_name,
            defaultextension=".pdf",
            filetypes=[("PDF Document", "*.pdf")],
        )
        if not out_path:
            return

        self.var_status.set(f"Exporting PDF to {out_path}...")
        self.root.config(cursor="watch")
        self.root.update_idletasks()

        try:
            options = self._build_conversion_options()
            res = render_pdf(md_text, str(self.base_dir), out_path, options)
        except Exception as exc:
            self.root.config(cursor="")
            messagebox.showerror(
                "PDF Export Failed",
                f"An error occurred while exporting the PDF:\n{exc}",
                parent=self.root,
            )
            self.var_status.set("PDF export failed.")
            return

        self.root.config(cursor="")
        suppressed_note = (
            f"\n• Suppressed missing images: {len(res.suppressed_images)}"
            if res.suppressed_images
            else ""
        )
        self.var_status.set(
            f"Exported {res.output_pdf_path} ({res.page_count} pages, "
            f"{res.embedded_images} images) in {res.elapsed_seconds:.2f}s"
        )

        open_now = messagebox.askyesno(
            "PDF Export Complete",
            f"Successfully exported PDF!\n\n"
            f"• File: {res.output_pdf_path}\n"
            f"• Total Pages: {res.page_count}\n"
            f"• Embedded Images: {res.embedded_images}"
            f"{suppressed_note}\n\n"
            f"Would you like to open the PDF file now?",
            parent=self.root,
        )
        if open_now:
            try:
                if sys.platform == "win32":
                    os.startfile(res.output_pdf_path)
                elif sys.platform.startswith("linux"):
                    subprocess.Popen(["xdg-open", res.output_pdf_path])
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Editor Events & Syntax Highlighting
    # ------------------------------------------------------------------

    def _update_header_labels(self) -> None:
        name = self.current_file.name if self.current_file else "Untitled.md"
        mod = " • Modified" if self.is_modified else ""
        self.lbl_editor_title.configure(text=f"Markdown Editor — {name}{mod}")
        self.var_base_dir_label.set(f"Base Dir: {self.base_dir}")

    def _on_editor_modified(self, _event: Optional[tk.Event] = None) -> None:
        if not self.editor.edit_modified():
            return
        self.editor.edit_modified(False)
        if not self.is_modified:
            self.is_modified = True
            self._update_header_labels()

        # Debounce syntax highlighting
        if self._syntax_after_id:
            self.root.after_cancel(self._syntax_after_id)
        self._syntax_after_id = self.root.after(250, self._apply_syntax_highlighting)

        # Debounce live preview refresh
        if self.var_auto_preview.get():
            if self._debounce_after_id:
                self.root.after_cancel(self._debounce_after_id)
            self._debounce_after_id = self.root.after(
                1200, self.trigger_preview_render
            )

    def _on_option_changed(self) -> None:
        if self.editor.get("1.0", "end-1c").strip():
            if self._debounce_after_id:
                self.root.after_cancel(self._debounce_after_id)
            self._debounce_after_id = self.root.after(300, self.trigger_preview_render)

    def _apply_syntax_highlighting(self) -> None:
        for tag in ("md_h1", "md_h2", "md_h3", "md_bold", "md_img", "md_code", "md_table"):
            self.editor.tag_remove(tag, "1.0", tk.END)

        text = self.editor.get("1.0", "end-1c")
        for line_idx, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("# "):
                self.editor.tag_add("md_h1", f"{line_idx}.0", f"{line_idx}.end")
            elif stripped.startswith("## "):
                self.editor.tag_add("md_h2", f"{line_idx}.0", f"{line_idx}.end")
            elif stripped.startswith("### "):
                self.editor.tag_add("md_h3", f"{line_idx}.0", f"{line_idx}.end")
            elif stripped.startswith("|") and stripped.endswith("|"):
                self.editor.tag_add("md_table", f"{line_idx}.0", f"{line_idx}.end")

            for m in re.finditer(r"\*\*[^*]+\*\*", line):
                self.editor.tag_add(
                    "md_bold", f"{line_idx}.{m.start()}", f"{line_idx}.{m.end()}"
                )
            for m in re.finditer(r"!\[[^\]]*\]\([^)]+\)", line):
                self.editor.tag_add(
                    "md_img", f"{line_idx}.{m.start()}", f"{line_idx}.{m.end()}"
                )
            for m in re.finditer(r"`[^`]+`", line):
                self.editor.tag_add(
                    "md_code", f"{line_idx}.{m.start()}", f"{line_idx}.{m.end()}"
                )

    # ------------------------------------------------------------------
    # Background PDF Preview Rendering & Canvas Painting
    # ------------------------------------------------------------------

    def trigger_preview_render(self) -> None:
        md_text = self.editor.get("1.0", "end-1c")
        if not md_text.strip():
            self._raw_pil_pages.clear()
            self._tk_page_images.clear()
            self.var_page_badge.set("No preview rendered")
            self._draw_empty_preview_placeholder()
            return

        self._render_generation += 1
        gen = self._render_generation
        options = self._build_conversion_options()
        base_dir_str = str(self.base_dir)
        out_pdf_str = str(self._preview_pdf_path)

        self.var_status.set("Rendering print-styled A4 preview...")
        self.var_page_badge.set("Rendering...")

        worker = threading.Thread(
            target=self._background_render_worker,
            args=(gen, md_text, base_dir_str, out_pdf_str, options),
            daemon=True,
        )
        worker.start()

    def _background_render_worker(
        self,
        gen: int,
        md_text: str,
        base_dir_str: str,
        out_pdf_str: str,
        options: ConversionOptions,
    ) -> None:
        try:
            # Use unique preview file per generation to avoid file locks
            gen_pdf_path = str(self._temp_dir / f"_preview_{gen}.pdf")
            res = render_pdf(md_text, base_dir_str, gen_pdf_path, options)
            pil_pages = render_pdf_pages_to_pil(gen_pdf_path, dpi=115)
            try:
                os.remove(gen_pdf_path)
            except OSError:
                pass
            self._render_queue.put(("success", gen, res, pil_pages))
        except Exception as exc:
            self._render_queue.put(("error", gen, str(exc), []))

    def _poll_render_queue(self) -> None:
        try:
            while True:
                status, gen, payload, pil_pages = self._render_queue.get_nowait()
                if gen != self._render_generation:
                    continue
                if status == "success":
                    res: ConversionResult = payload
                    self._last_rendered_result = res
                    self._raw_pil_pages = pil_pages
                    self.var_page_badge.set(
                        f"{res.page_count} page{'s' if res.page_count != 1 else ''} "
                        f"| {res.embedded_images} image{'s' if res.embedded_images != 1 else ''}"
                    )
                    suppressed_info = (
                        f" | Suppressed {len(res.suppressed_images)} missing image(s)"
                        if res.suppressed_images
                        else ""
                    )
                    self.var_status.set(
                        f"Preview ready: {res.page_count} pages, "
                        f"{res.embedded_images} embedded images "
                        f"({res.elapsed_seconds:.2f}s){suppressed_info}"
                    )
                    self._redraw_preview_canvas()
                else:
                    self.var_page_badge.set("Render error")
                    self.var_status.set(f"Preview error: {payload}")
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_render_queue)

    def _draw_empty_preview_placeholder(self) -> None:
        self.preview_canvas.delete("all")
        w = max(400, self.preview_canvas.winfo_width())
        h = max(300, self.preview_canvas.winfo_height())
        self.preview_canvas.create_text(
            w // 2,
            h // 2 - 12,
            text="A4 Print Preview Studio",
            fill="#cbd5e1",
            font=("Segoe UI", 14, "bold"),
        )
        self.preview_canvas.create_text(
            w // 2,
            h // 2 + 18,
            text="Click 'Open .md...' or type Markdown on the left, then press F5 to preview.",
            fill="#94a3b8",
            font=("Segoe UI", 10),
        )
        self.preview_canvas.configure(scrollregion=(0, 0, w, h))

    def _redraw_preview_canvas(self) -> None:
        if not self._raw_pil_pages:
            self._draw_empty_preview_placeholder()
            return

        self.preview_canvas.delete("all")
        self._tk_page_images.clear()
        self._page_y_coords.clear()

        canvas_w = max(400, self.preview_canvas.winfo_width())
        y_cursor = 24
        max_content_w = canvas_w
        total_pages = len(self._raw_pil_pages)

        for idx, pil_img in enumerate(self._raw_pil_pages, start=1):
            scaled_w = max(100, int(pil_img.width * self._zoom_level))
            scaled_h = max(140, int(pil_img.height * self._zoom_level))

            if abs(self._zoom_level - 1.0) > 0.01:
                resized = pil_img.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
            else:
                resized = pil_img

            tk_img = ImageTk.PhotoImage(resized)
            self._tk_page_images.append(tk_img)

            x_center = max(scaled_w // 2 + 24, canvas_w // 2)
            max_content_w = max(max_content_w, scaled_w + 48)
            x0 = x_center - scaled_w // 2
            y0 = y_cursor

            self._page_y_coords.append(max(0, y0 - 16))

            # Subtle drop shadow behind A4 page card
            self.preview_canvas.create_rectangle(
                x0 + 5,
                y0 + 5,
                x0 + scaled_w + 5,
                y0 + scaled_h + 5,
                fill="#1e293b",
                outline="",
            )
            # Page image
            self.preview_canvas.create_image(x0, y0, anchor=tk.NW, image=tk_img)
            # Page number caption
            self.preview_canvas.create_text(
                x_center,
                y0 + scaled_h + 14,
                text=f"Page {idx} of {total_pages}",
                fill="#cbd5e1",
                font=("Segoe UI", 8, "bold"),
            )

            y_cursor += scaled_h + 38

        self.preview_canvas.configure(scrollregion=(0, 0, max_content_w, y_cursor))

    # ------------------------------------------------------------------
    # Zoom & Navigation Controls
    # ------------------------------------------------------------------

    def set_zoom(self, zoom: float) -> None:
        self._zoom_level = max(0.4, min(2.2, zoom))
        self.var_zoom_label.set(f"{int(round(self._zoom_level * 100))}%")
        self._redraw_preview_canvas()

    def adjust_zoom(self, delta: float) -> None:
        self.set_zoom(self._zoom_level + delta)

    def fit_preview_to_width(self) -> None:
        if not self._raw_pil_pages:
            return
        canvas_w = max(300, self.preview_canvas.winfo_width() - 56)
        page_w = self._raw_pil_pages[0].width
        self.set_zoom(canvas_w / page_w)

    def scroll_to_page_delta(self, delta: int) -> None:
        if not self._page_y_coords:
            return
        sr = self.preview_canvas.cget("scrollregion").split()
        if len(sr) != 4:
            return
        total_h = max(1, float(sr[3]))
        curr_top = self.preview_canvas.yview()[0] * total_h

        # Find current visible page index
        current_idx = 0
        for i, y in enumerate(self._page_y_coords):
            if y <= curr_top + 40:
                current_idx = i
            else:
                break

        target_idx = max(0, min(len(self._page_y_coords) - 1, current_idx + delta))
        target_y = self._page_y_coords[target_idx]
        self.preview_canvas.yview_moveto(target_y / total_h)

    def _on_preview_mousewheel(self, event: tk.Event) -> None:
        if event.state & 0x0004:  # Ctrl + MouseWheel zooms
            self.adjust_zoom(0.1 if event.delta > 0 else -0.1)
        else:
            self.preview_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_linux_scroll(self, event: tk.Event, direction: int) -> None:
        if event.state & 0x0004:  # Ctrl + Scroll zooms
            self.adjust_zoom(-0.1 if direction > 0 else 0.1)
        else:
            self.preview_canvas.yview_scroll(direction, "units")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        if not self._confirm_discard_changes():
            return
        if self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self.root.destroy()


def main() -> None:
    _enable_windows_hidpi()
    root = tk.Tk()
    initial_arg = sys.argv[1] if len(sys.argv) > 1 else None
    MdToPdfStudioApp(root, initial_file=initial_arg)
    root.mainloop()


if __name__ == "__main__":
    main()
