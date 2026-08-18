#!/usr/bin/env python3
"""Drag-and-drop GUI for translating an EPUB via the Claude Code or Codex CLI.

Bootstraps its own dependencies (tkinterdnd2 for drag-and-drop, tkinterweb for
the rendered preview, plus the epub parsing libraries) the same way
translate_epub.py does, then shows a window: drop or browse for an .epub,
pick an engine and a language, click Translate. Once translated, every chunk
shows up in a list on the left with its own Re-translate button (which uses
whichever engine is currently selected, so a chunk can be retried with the
other engine); clicking a chunk renders its current (translated) HTML in a
preview pane on the right.

Threading model: worker threads only ever call claude_driver/codex_driver
(network-bound, side-effect-free) and hand results back over a queue. All
BeautifulSoup tree mutation (epub_io.apply_translations) and epub writing
happen on the main thread (in response to queued messages), so nothing races
with the preview pane reading the same tree."""

from __future__ import annotations

import queue
import re
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import claude_driver  # stdlib-only, safe to import before dependency checks
import codex_driver  # stdlib-only, safe to import before dependency checks
import local_ai_driver  # stdlib-only, safe to import before dependency checks
import preflight  # stdlib-only, safe to import before dependency checks
import translation_common  # stdlib-only, safe to import before dependency checks

GUI_DEPS = [
    ("tkinterdnd2", "tkinterdnd2"),
    ("tkinterweb", "tkinterweb"),
    ("ttkbootstrap", "ttkbootstrap"),
] + preflight.EPUB_DEPS

THEME = "superhero"
APP_NAME = "INNO AI Agent Translator"

# claude/codex ride an existing CLI login (checked via preflight.ENGINES); local_ai has no
# such thing -- it's a plain HTTP client against a URL/key the user configures themselves,
# so it's handled separately wherever engine-specific setup/readiness logic comes up.
DRIVERS = {"claude": claude_driver, "codex": codex_driver, "local_ai": local_ai_driver}
ENGINE_KEYS = ["claude", "codex", "local_ai"]
ENGINE_LABELS = {**{key: preflight.ENGINES[key]["label"] for key in ("claude", "codex")}, "local_ai": "Local AI"}
ENGINE_KEYS_BY_LABEL = {label: key for key, label in ENGINE_LABELS.items()}

COMMON_LANGUAGES = [
    "Spanish", "French", "German", "Italian", "Portuguese (Brazil)",
    "Japanese", "Korean", "Simplified Chinese", "Arabic", "Russian", "Hindi",
]

MAX_TOKENS_PER_CHUNK = 6000
CONCURRENCY = 3

STATUS_LABELS = {
    "queued": ("queued", "secondary"),
    "translating": ("translating…", "warning"),
    "done": ("done", "success"),
    "failed": ("failed", "danger"),
}


def _ensure_gui_deps() -> None:
    missing = preflight.missing_modules(GUI_DEPS)
    if not missing:
        return
    print(f"Installing missing Python dependencies: {', '.join(missing)}...")
    if not preflight.install_modules(missing):
        print(
            "error: automatic install failed. Set up a virtualenv, run "
            "`pip install -r requirements.txt`, then re-run this app.",
            file=sys.stderr,
        )
        sys.exit(1)


def _parse_dnd_paths(data: str) -> list:
    """tkinterdnd2 hands back a Tcl list string: space-separated, with any
    path containing spaces wrapped in {braces}."""
    paths = []
    i, n = 0, len(data)
    while i < n:
        while i < n and data[i] == " ":
            i += 1
        if i >= n:
            break
        if data[i] == "{":
            j = data.index("}", i)
            paths.append(data[i + 1:j])
            i = j + 1
        else:
            j = data.find(" ", i)
            if j == -1:
                j = n
            paths.append(data[i:j])
            i = j
    return paths


def derive_output_path(source: Path, target_lang: str) -> Path:
    tag = re.sub(r"[^A-Za-z0-9]+", "-", target_lang.strip()).strip("-").lower() or "translated"
    return source.parent / f"{source.stem}.{tag}.epub"


class ChunkInfo:
    """Tracks one chunk across the app's lifetime: its units (live references
    into the book's soup trees), status, and its UI row widgets."""

    def __init__(self, index, units, tokens):
        self.index = index
        self.units = units
        self.tokens = tokens
        self.status = "queued"  # queued | translating | done | failed
        self.error = None
        self.retranslating = False
        self.row_frame = None
        self.info_label = None
        self.status_label = None
        self.retranslate_button = None

    def current_html(self) -> str:
        return "\n".join(str(u.tag) for u in self.units)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1080x640")
        self.root.minsize(860, 520)

        self.source_path = None
        self.msg_queue = queue.Queue()
        self.full_translate_active = False
        self._engine_ready = False
        self._checked_engine = None  # which engine the last status check was for

        # Local AI has no CLI login -- just a URL/key/model the user configures via the
        # gear icon. Kept in memory only for this session, never written to disk.
        self.local_ai_base_url = ""
        self.local_ai_api_key = ""
        self.local_ai_model = ""

        self.book = None
        self.chunks = []  # list[ChunkInfo]
        self.selected_chunk = None
        self.output_path = None
        self.target_lang = ""
        self.source_lang = ""
        self._write_lock = threading.Lock()

        self._start_time = None
        self._total_tokens = 0
        self._completed_tokens = 0
        self._total_chunks = 0
        self._completed_chunks = 0

        self._build_ui()
        self._poll_queue()
        self._check_engine_async(self._engine_key())

    # ---- UI construction ----

    def _build_ui(self):
        import ttkbootstrap as tb

        colors = tb.Style().colors

        # Two-column layout: a fixed-width sidebar on the left holds the setup controls
        # (engine, login, from/to language, save-as) -- none of that needs to stretch
        # across the whole window, it was just filling the space because it was the only
        # thing in a full-width row. The chunk list + preview -- the "chapters and
        # details" that actually grow with the book -- get the rest of the window on
        # the right, full height, instead of being squeezed into a strip at the bottom.
        main = tb.Frame(self.root)
        main.pack(fill="both", expand=True)

        sidebar = tb.Frame(main, width=320, padding=(20, 20, 12, 20))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        content = tb.Frame(main, padding=(8, 20, 20, 20))
        content.pack(side="left", fill="both", expand=True)

        # Header
        tb.Label(sidebar, text=APP_NAME, font=("", 16, "bold"),
                 wraplength=280, justify="left").pack(anchor="w")
        tb.Label(sidebar, text="Claude, Codex, or a local model -- formatting intact.",
                 bootstyle="secondary", wraplength=280, justify="left").pack(anchor="w", pady=(2, 16))

        # Setup card: engine (+ its status/login), language, output filename
        setup = tb.Labelframe(sidebar, text="Setup", padding=14)
        setup.pack(fill="x", pady=(0, 12))
        setup.columnconfigure(1, weight=1)

        tb.Label(setup, text="Engine").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=(0, 8))
        self.engine_var = tk.StringVar(value=ENGINE_LABELS["claude"])
        self.engine_combo = tb.Combobox(setup, textvariable=self.engine_var, state="readonly",
                                         values=[ENGINE_LABELS[k] for k in ENGINE_KEYS])
        self.engine_combo.grid(row=0, column=1, sticky="ew", pady=(0, 8))
        self.engine_var.trace_add("write", lambda *_: self._on_engine_changed())

        self.engine_settings_button = tb.Button(setup, text="⚙", width=3, bootstyle="secondary-outline",
                                                  command=self._open_local_ai_settings)
        self.engine_settings_button.grid(row=0, column=2, padx=(6, 0), pady=(0, 8))
        self.engine_settings_button.grid_remove()

        self.engine_status_var = tk.StringVar(value="Checking...")
        self.engine_status_label = tb.Label(setup, textvariable=self.engine_status_var,
                                             bootstyle="secondary", wraplength=280, justify="left")
        self.engine_status_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self.login_button = tb.Button(setup, text="Log in", bootstyle="warning", command=self._start_login,
                                       state="disabled")
        self.login_button.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 4))

        tb.Separator(setup).grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)

        tb.Label(setup, text="From").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=(0, 8))
        self.source_lang_var = tk.StringVar(value=translation_common.AUTO_DETECT)
        self.source_lang_combo = tb.Combobox(setup, textvariable=self.source_lang_var,
                                              values=[translation_common.AUTO_DETECT] + COMMON_LANGUAGES)
        self.source_lang_combo.grid(row=4, column=1, sticky="ew", pady=(0, 8))

        tb.Label(setup, text="To").grid(row=5, column=0, sticky="w", padx=(0, 12), pady=(0, 8))
        self.lang_var = tk.StringVar()
        self.lang_combo = tb.Combobox(setup, textvariable=self.lang_var, values=COMMON_LANGUAGES)
        self.lang_combo.grid(row=5, column=1, sticky="ew", pady=(0, 8))
        self.lang_var.trace_add("write", lambda *_: self._on_language_changed())

        tb.Label(setup, text="Save as").grid(row=6, column=0, sticky="w", padx=(0, 12))
        self.output_name_var = tk.StringVar()
        self._last_auto_name = ""
        self.output_name_entry = tb.Entry(setup, textvariable=self.output_name_var)
        self.output_name_entry.grid(row=6, column=1, sticky="ew")

        # Drop zone -- uses inputbg/secondary rather than light/border: those two are
        # reliably distinct from the page background in both light and dark themes,
        # whereas border in particular is literally identical to bg in dark themes
        # (verified for "darkly": both #222222), which made the box invisible.
        self.drop_frame = tk.Frame(sidebar, bg=colors.inputbg, highlightbackground=colors.secondary,
                                    highlightthickness=1, height=88)
        self.drop_frame.pack(fill="x", pady=8)
        self.drop_label = tk.Label(self.drop_frame, text="Drag an .epub file here, or click to browse",
                                    bg=colors.inputbg, fg=colors.secondary, font=("", 11), cursor="hand2",
                                    wraplength=270, justify="center")
        self.drop_label.pack(expand=True, fill="both", pady=14)
        self.drop_label.bind("<Button-1>", lambda e: self._browse())
        self.drop_frame.bind("<Button-1>", lambda e: self._browse())

        try:
            from tkinterdnd2 import DND_FILES
            for widget in (self.drop_label, self.drop_frame):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass  # drag-and-drop unavailable; clicking to browse still works

        self.translate_button = tb.Button(sidebar, text="Translate", bootstyle="primary",
                                           command=self._on_translate, state="disabled")
        self.translate_button.pack(fill="x", pady=(6, 10))

        self.progress = tb.Progressbar(sidebar, mode="determinate", bootstyle="primary")
        self.progress.pack(fill="x", pady=(0, 4))

        self.status_var = tk.StringVar(value="")
        tb.Label(sidebar, textvariable=self.status_var, bootstyle="secondary",
                 wraplength=280, justify="left").pack(anchor="w", fill="x", pady=(0, 8))

        # Chunk list (left) + rendered preview (right) -- "chapters and details" for
        # the loaded book, filling the entire right side of the window.
        paned = tb.PanedWindow(content, orient="horizontal")
        paned.pack(fill="both", expand=True)

        chunks_container = tb.Frame(paned)
        paned.add(chunks_container, weight=2)

        # ttk.Panedwindow sizes each pane from its requested/natural size on first
        # layout (weight= only governs how *extra* space is redistributed on later
        # resizes), and a bare Canvas has no natural width of its own -- without an
        # explicit width hint here the initial split can leave this pane too narrow
        # to fit a row's status badge and Retry button.
        self.chunks_canvas = tk.Canvas(chunks_container, highlightthickness=0, bg=colors.bg, width=420)
        chunks_scrollbar = tb.Scrollbar(chunks_container, orient="vertical", command=self.chunks_canvas.yview)
        self.chunks_inner = tb.Frame(self.chunks_canvas)
        self.chunks_inner.bind(
            "<Configure>",
            lambda e: self.chunks_canvas.configure(scrollregion=self.chunks_canvas.bbox("all")),
        )
        self.chunks_window_id = self.chunks_canvas.create_window((0, 0), window=self.chunks_inner, anchor="nw")

        def _on_chunks_canvas_resize(event):
            # Keep the inner frame's width tied to the canvas's actual visible width, so rows
            # use the real available space instead of clipping at whatever width they'd
            # naturally request (the canvas doesn't do this on its own for an embedded window).
            self.chunks_canvas.itemconfig(self.chunks_window_id, width=event.width)
            # The placeholder's wraplength must track the same real width, or it clips
            # instead of wrapping whenever the pane is narrower than a hardcoded guess.
            self.chunks_placeholder.config(wraplength=max(80, event.width - 16))

        self.chunks_canvas.bind("<Configure>", _on_chunks_canvas_resize)
        self.chunks_canvas.configure(yscrollcommand=chunks_scrollbar.set)
        self.chunks_canvas.pack(side="left", fill="both", expand=True)
        chunks_scrollbar.pack(side="right", fill="y")
        self._bind_scroll(self.chunks_canvas)

        self.chunks_placeholder = tb.Label(
            self.chunks_inner, text="Chunks will appear here once you click Translate.",
            bootstyle="secondary", wraplength=260, justify="left",
        )
        self.chunks_placeholder.pack(padx=8, pady=8, anchor="w")

        # A visible border around the preview frame: the HTML preview stays a plain
        # white/light "page" regardless of app theme (book content reads best on a
        # neutral light background), so it needs a clear frame around it in a dark
        # theme -- otherwise it looks like a stray rendering glitch instead of a
        # deliberate reading pane.
        preview_container = tk.Frame(paned, highlightbackground=colors.secondary, highlightthickness=1)
        paned.add(preview_container, weight=2)

        from tkinterweb import HtmlFrame
        # width/height are just initial-layout hints (pack(fill="both", expand=True)
        # below still lets it grow to whatever the pane actually gets) -- tkinterweb's
        # default requested size is ~800x600, which is bigger than the chunk list's,
        # so with equal pane weights below, ttk.Panedwindow's deficit-splitting (equal
        # weight = equal absolute pixels trimmed from each pane's *requested* size, not
        # proportional) crushed the narrower chunk-list pane down to ~140px before this.
        self.preview_frame = HtmlFrame(preview_container, messages_enabled=False, width=300, height=200)
        self.preview_frame.pack(fill="both", expand=True)
        # Only tinted while empty -- once there's real translated content to show,
        # it switches to a plain light page (book text reads best that way), but an
        # unused panel showing stark white next to a dark theme looks like a bug.
        self._preview_bg = colors.bg
        self._render_preview_empty()

    def _bind_scroll(self, widget):
        def on_wheel(event):
            if getattr(event, "num", None) == 4:
                widget.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                widget.yview_scroll(1, "units")
            else:
                widget.yview_scroll(int(-1 * (event.delta / 120)), "units")

        widget.bind("<MouseWheel>", on_wheel)
        widget.bind("<Button-4>", on_wheel)
        widget.bind("<Button-5>", on_wheel)

    # ---- file selection ----

    def _browse(self):
        if self.full_translate_active:
            return
        path = filedialog.askopenfilename(title="Choose an EPUB", filetypes=[("EPUB files", "*.epub")])
        if path:
            self._set_file(Path(path))

    def _on_drop(self, event):
        if self.full_translate_active:
            return
        paths = _parse_dnd_paths(event.data)
        epub_paths = [p for p in paths if p.lower().endswith(".epub")]
        if not epub_paths:
            messagebox.showerror("Not an EPUB", "Drop a .epub file.")
            return
        self._set_file(Path(epub_paths[0]))

    def _set_file(self, path: Path):
        self.source_path = path
        self.drop_label.config(text=f"Selected:\n{path.name}")
        self._refresh_output_name()
        self._clear_chunks()
        self._update_translate_state()
        threading.Thread(target=self._preview_stats, args=(path,), daemon=True).start()

    def _refresh_output_name(self):
        if self.source_path is None:
            return
        suggested = derive_output_path(self.source_path, self.lang_var.get().strip()).name
        current = self.output_name_var.get().strip()
        # Only overwrite if the field is empty or still matches our last suggestion --
        # i.e. don't clobber a name the user typed themselves.
        if current in ("", self._last_auto_name):
            self.output_name_var.set(suggested)
            self._last_auto_name = suggested

    def _on_language_changed(self):
        self._refresh_output_name()
        self._update_translate_state()

    def _preview_stats(self, path: Path):
        try:
            import epub_io
            book = epub_io.load_book(str(path))
            pages = epub_io.estimate_pages(book.units)
            self.msg_queue.put(("preview", (path, len(book.chapters), pages)))
        except Exception:
            pass  # not fatal -- Translate will surface a real error if the file is actually bad

    # ---- engine status ----

    def _engine_key(self) -> str:
        return ENGINE_KEYS_BY_LABEL.get(self.engine_var.get(), "claude")

    def _on_engine_changed(self):
        engine = self._engine_key()
        self._engine_ready = False
        self._update_translate_state()

        if engine == "local_ai":
            self.login_button.grid_remove()
            self.engine_settings_button.grid()
            self._refresh_local_ai_status()
        else:
            self.engine_settings_button.grid_remove()
            self.login_button.grid()
            self.login_button.config(state="disabled")
            self.engine_status_var.set("Checking...")
            self._check_engine_async(engine)

    def _check_engine_async(self, engine: str):
        info = preflight.ENGINES[engine]

        def work():
            if not info["is_installed"]():
                self.msg_queue.put(("engine_status", (engine, "not_installed", None)))
                return
            status = info["auth_status"]()
            if status.get("loggedIn"):
                detail = status.get("email") or status.get("detail") or ""
                self.msg_queue.put(("engine_status", (engine, "ok", detail)))
            else:
                self.msg_queue.put(("engine_status", (engine, "not_logged_in", None)))

        threading.Thread(target=work, daemon=True).start()

    def _start_login(self):
        engine = self._engine_key()
        info = preflight.ENGINES[engine]
        self.login_button.config(state="disabled")
        self.engine_status_var.set(f"Opening {info['label']} login (check your browser/terminal)...")

        def work():
            status = info["login"]()
            if status.get("loggedIn"):
                detail = status.get("email") or status.get("detail") or ""
                self.msg_queue.put(("engine_status", (engine, "ok", detail)))
            else:
                self.msg_queue.put(("engine_status", (engine, "not_logged_in", None)))

        threading.Thread(target=work, daemon=True).start()

    # ---- local AI settings ----

    def _refresh_local_ai_status(self):
        if self.local_ai_base_url and self.local_ai_model:
            self._engine_ready = True
            self.engine_status_var.set(f"Local AI: {self.local_ai_model} @ {self.local_ai_base_url}")
        else:
            self._engine_ready = False
            self.engine_status_var.set("Local AI: not configured -- click ⚙ to set the URL, API key, and model.")
        self._update_translate_state()

    def _open_local_ai_settings(self):
        import ttkbootstrap as tb

        dialog = tb.Toplevel(self.root)
        dialog.title("Local AI settings")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        form = tb.Frame(dialog, padding=16)
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, weight=1, minsize=280)

        tb.Label(form, text="Base URL").grid(row=0, column=0, sticky="w", pady=(0, 8), padx=(0, 12))
        base_url_var = tk.StringVar(value=self.local_ai_base_url)
        tb.Entry(form, textvariable=base_url_var).grid(row=0, column=1, sticky="ew", pady=(0, 8))

        tb.Label(form, text="API Key").grid(row=1, column=0, sticky="w", pady=(0, 8), padx=(0, 12))
        api_key_var = tk.StringVar(value=self.local_ai_api_key)
        tb.Entry(form, textvariable=api_key_var, show="*").grid(row=1, column=1, sticky="ew", pady=(0, 8))

        tb.Label(form, text="Model").grid(row=2, column=0, sticky="w", pady=(0, 8), padx=(0, 12))
        model_var = tk.StringVar(value=self.local_ai_model)
        model_combo = tb.Combobox(form, textvariable=model_var, values=[])
        model_combo.grid(row=2, column=1, sticky="ew", pady=(0, 8))

        test_status_var = tk.StringVar(value="")
        tb.Label(form, textvariable=test_status_var, bootstyle="secondary",
                 wraplength=360, justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 8))

        button_row = tb.Frame(form)
        button_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        test_button = tb.Button(button_row, text="Test connection", bootstyle="info-outline")
        test_button.pack(side="left")

        def do_test():
            base_url = base_url_var.get().strip()
            api_key = api_key_var.get().strip()
            if not base_url:
                test_status_var.set("Enter a base URL first.")
                return
            test_button.config(state="disabled")
            test_status_var.set("Testing...")
            result_queue = queue.Queue()

            def work():
                try:
                    models = local_ai_driver.list_models(base_url, api_key)
                    result_queue.put(("ok", models))
                except Exception as exc:
                    result_queue.put(("error", str(exc)))

            threading.Thread(target=work, daemon=True).start()

            def poll():
                try:
                    kind, payload = result_queue.get_nowait()
                except queue.Empty:
                    dialog.after(100, poll)
                    return
                test_button.config(state="normal")
                if kind == "ok":
                    test_status_var.set(f"Connected. {len(payload)} model(s) found.")
                    model_combo.config(values=payload)
                    if payload and not model_var.get():
                        model_var.set(payload[0])
                else:
                    test_status_var.set(f"Failed: {payload}")

            dialog.after(100, poll)

        test_button.config(command=do_test)

        def do_save():
            self.local_ai_base_url = base_url_var.get().strip()
            self.local_ai_api_key = api_key_var.get().strip()
            self.local_ai_model = model_var.get().strip()
            if self._engine_key() == "local_ai":
                self._refresh_local_ai_status()
            dialog.destroy()

        tb.Button(button_row, text="Save", bootstyle="primary", command=do_save).pack(side="right")
        tb.Button(button_row, text="Cancel", bootstyle="secondary",
                  command=dialog.destroy).pack(side="right", padx=(0, 8))

    # ---- chunk list ----

    def _clear_chunks(self):
        for chunk in self.chunks:
            if chunk.row_frame is not None:
                chunk.row_frame.destroy()
        self.chunks = []
        self.selected_chunk = None
        self.book = None
        self.chunks_placeholder.pack(padx=8, pady=8, anchor="w")
        self._render_preview_empty()

    def _populate_chunks(self, raw_chunks, chunk_tokens):
        self.chunks_placeholder.pack_forget()
        self.chunks = []
        for i, (units, tokens) in enumerate(zip(raw_chunks, chunk_tokens), start=1):
            chunk = ChunkInfo(i, units, tokens)
            self._build_chunk_row(chunk)
            self.chunks.append(chunk)
        # Force the inner frame to the canvas's current width immediately, rather than
        # waiting for a <Configure> event that may not have fired yet (e.g. if chunks are
        # populated very soon after the window is created).
        self.chunks_canvas.itemconfig(self.chunks_window_id, width=self.chunks_canvas.winfo_width())

    def _build_chunk_row(self, chunk: "ChunkInfo"):
        import ttkbootstrap as tb

        row = tb.Frame(self.chunks_inner, padding=(8, 6))
        row.pack(fill="x", padx=2, pady=1)

        info_label = tb.Label(row, text=f"Chunk {chunk.index}   ·   ~{chunk.tokens:,} tok",
                               cursor="hand2", anchor="w")
        info_label.pack(side="left", fill="x", expand=True)
        info_label.bind("<Button-1>", lambda e, c=chunk: self._select_chunk(c))

        status_label = tb.Label(row, text="queued", bootstyle="secondary-inverse",
                                 width=13, anchor="center")
        status_label.pack(side="left", padx=(4, 8))

        retranslate_button = tb.Button(row, text="Retry", bootstyle="outline-secondary", width=7,
                                        command=lambda c=chunk: self._on_retranslate_chunk(c))
        retranslate_button.pack(side="left")

        chunk.row_frame = row
        chunk.info_label = info_label
        chunk.status_label = status_label
        chunk.retranslate_button = retranslate_button

    def _refresh_chunk_row(self, chunk: "ChunkInfo"):
        text, style = STATUS_LABELS.get(chunk.status, (chunk.status, "secondary"))
        if chunk.status_label is not None:
            chunk.status_label.config(text=text, bootstyle=f"{style}-inverse")
        if chunk.retranslate_button is not None:
            busy = chunk.retranslating or self.full_translate_active
            chunk.retranslate_button.config(state="disabled" if busy else "normal")

    def _refresh_all_chunk_rows(self):
        for chunk in self.chunks:
            self._refresh_chunk_row(chunk)

    def _select_chunk(self, chunk: "ChunkInfo"):
        if self.selected_chunk is not None and self.selected_chunk.info_label is not None:
            self.selected_chunk.info_label.config(font=("", 10, "normal"))
        self.selected_chunk = chunk
        if chunk.info_label is not None:
            chunk.info_label.config(font=("", 10, "bold"))
        self._render_chunk_preview(chunk)

    def _render_chunk_preview(self, chunk: "ChunkInfo"):
        self._render_preview_html(f"<html><body>{chunk.current_html()}</body></html>")

    def _render_preview_empty(self):
        self._render_preview_html(f'<html><body style="background:{self._preview_bg};"></body></html>')

    def _render_preview_html(self, html: str):
        try:
            self.preview_frame.load_html(html)
        except Exception:
            pass

    # ---- translate (whole book) ----

    def _update_translate_state(self):
        any_chunk_busy = any(c.retranslating for c in self.chunks)
        ready = (
            self.source_path is not None
            and self.lang_var.get().strip() != ""
            and not self.full_translate_active
            and not any_chunk_busy
            and self._engine_ready
        )
        self.translate_button.config(state="normal" if ready else "disabled")

    def _on_translate(self):
        if self.full_translate_active or self.source_path is None:
            return
        target_lang = self.lang_var.get().strip()
        if not target_lang:
            return
        source_lang = self.source_lang_var.get().strip()
        engine = self._engine_key()

        output_name = self.output_name_var.get().strip()
        if not output_name:
            output_name = derive_output_path(self.source_path, target_lang).name
        if not output_name.lower().endswith(".epub"):
            output_name += ".epub"
        output_path = self.source_path.parent / output_name

        if output_path.resolve() == self.source_path.resolve():
            messagebox.showerror(
                "Same filename",
                "The output filename is the same as the source file. Choose a different "
                "name so the original isn't overwritten.",
            )
            return

        self.target_lang = target_lang
        self.source_lang = source_lang
        self.engine = engine
        self.output_path = output_path
        self.full_translate_active = True
        self.translate_button.config(state="disabled")
        self.progress.config(value=0, maximum=1)
        self.status_var.set(f"Parsing {self.source_path.name}...")
        self._clear_chunks()

        self._start_time = time.time()
        self._total_tokens = 0
        self._completed_tokens = 0
        self._total_chunks = 0
        self._completed_chunks = 0
        self._tick_timer()

        threading.Thread(
            target=self._parse_and_translate_all,
            args=(self.source_path, target_lang, source_lang, engine, output_path, self._engine_call_kwargs(engine)),
            daemon=True,
        ).start()

    def _engine_call_kwargs(self, engine: str) -> dict:
        """Extra keyword args a driver's translate_chunk needs beyond
        (units, target_lang, model) -- only local_ai needs anything here."""
        if engine == "local_ai":
            return {"base_url": self.local_ai_base_url, "api_key": self.local_ai_api_key}
        return {}

    def _engine_model(self, engine: str):
        return self.local_ai_model if engine == "local_ai" else None

    def _tick_timer(self):
        if not self.full_translate_active or self._start_time is None:
            return
        elapsed = int(time.time() - self._start_time)
        mins, secs = divmod(elapsed, 60)
        self.status_var.set(
            f"Chunk {self._completed_chunks}/{self._total_chunks} done · "
            f"{self._completed_tokens:,}/{self._total_tokens:,} tokens · "
            f"elapsed {mins}:{secs:02d}"
        )
        self.root.after(500, self._tick_timer)

    def _parse_and_translate_all(self, source_path, target_lang, source_lang, engine, output_path, engine_kwargs):
        try:
            import epub_io

            driver = DRIVERS[engine]
            model = self._engine_model(engine)
            book = epub_io.load_book(str(source_path))
            chunks = translation_common.pack_chunks(book.units, MAX_TOKENS_PER_CHUNK)
            chunk_tokens = [sum(translation_common.estimate_tokens(u.html) for u in chunk) for chunk in chunks]
            total_tokens = sum(chunk_tokens)
            pages = epub_io.estimate_pages(book.units)

            self.msg_queue.put(("book_ready", (book, chunks, chunk_tokens)))
            self.msg_queue.put(("status_line", (
                f"{len(book.chapters)} chapter(s), ~{pages} page(s), {len(book.units)} unit(s), "
                f"~{total_tokens:,} estimated tokens, packed into {len(chunks)} chunk(s)."
            )))
            self.msg_queue.put(("meta", (len(chunks), total_tokens)))

            def run_one(index, chunk, tokens):
                self.msg_queue.put(("chunk_started", index))
                translations, error = driver.translate_chunk(
                    chunk, target_lang, model, source_lang=source_lang, **engine_kwargs
                )
                return index, translations, error

            error_count = 0
            if chunks:
                with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
                    futures = [
                        pool.submit(run_one, i, chunk, chunk_tokens[i - 1])
                        for i, chunk in enumerate(chunks, start=1)
                    ]
                    completed = 0
                    completed_tokens = 0
                    for future in as_completed(futures):
                        index, translations, error = future.result()
                        completed += 1
                        completed_tokens += chunk_tokens[index - 1]
                        if error is not None:
                            error_count += 1
                        self.msg_queue.put(("chunk_result", (index, translations, error)))
                        self.msg_queue.put(("progress", (completed, completed_tokens)))

            self.msg_queue.put(("write_and_finish", (len(chunks), error_count)))
        except Exception as exc:
            self.msg_queue.put(("error", str(exc)))

    def _finish_full_translate(self, total_chunks, error_count):
        source_path, output_path, book = self.source_path, self.output_path, self.book

        def write():
            import epub_io
            with self._write_lock:
                epub_io.write_translated_epub(str(source_path), book.chapters, str(output_path))
            self.msg_queue.put(("full_translate_done", (str(output_path), error_count, total_chunks)))

        threading.Thread(target=write, daemon=True).start()

    # ---- per-chunk re-translate ----

    def _on_retranslate_chunk(self, chunk: "ChunkInfo"):
        if chunk.retranslating or self.full_translate_active:
            return
        engine = self._engine_key()
        if not self._engine_ready:
            reason = ("isn't configured yet -- click ⚙ to set it up." if engine == "local_ai"
                      else "isn't installed or logged in yet.")
            messagebox.showerror(f"{ENGINE_LABELS[engine]} not ready", f"{ENGINE_LABELS[engine]} {reason}")
            return
        target_lang = self.lang_var.get().strip() or self.target_lang
        if not target_lang:
            messagebox.showerror("No language", "Pick a target language first.")
            return
        source_lang = self.source_lang_var.get().strip() or self.source_lang

        chunk.retranslating = True
        chunk.status = "translating"
        self._refresh_chunk_row(chunk)
        self._update_translate_state()
        model = self._engine_model(engine)
        engine_kwargs = self._engine_call_kwargs(engine)

        def work():
            driver = DRIVERS[engine]
            translations, error = driver.translate_chunk(
                chunk.units, target_lang, model, source_lang=source_lang, **engine_kwargs
            )
            self.msg_queue.put(("chunk_result", (chunk.index, translations, error)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_chunk_result(self, index, translations, error) -> "ChunkInfo":
        chunk = self.chunks[index - 1]
        import epub_io
        try:
            epub_io.apply_translations(chunk.units, translations)
        except Exception as exc:
            error = error or exc
        chunk.status = "failed" if error else "done"
        chunk.error = error
        chunk.retranslating = False
        self._refresh_chunk_row(chunk)
        if self.selected_chunk is chunk:
            self._render_chunk_preview(chunk)
        self._update_translate_state()
        return chunk

    def _schedule_write(self):
        if self.book is None or self.output_path is None:
            return
        source_path, output_path, book = self.source_path, self.output_path, self.book

        def write():
            import epub_io
            with self._write_lock:
                epub_io.write_translated_epub(str(source_path), book.chapters, str(output_path))
            self.msg_queue.put(("write_complete", None))

        threading.Thread(target=write, daemon=True).start()

    # ---- queue polling ----

    def _poll_queue(self):
        try:
            while True:
                kind, *payload = self.msg_queue.get_nowait()
                self._handle_message(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_message(self, kind, payload):
        if kind == "preview":
            path, chapters, pages = payload[0]
            if path == self.source_path:  # ignore if the user picked a different file meanwhile
                self.drop_label.config(
                    text=f"Selected:\n{path.name}\n{chapters} chapter(s), ~{pages} page(s)"
                )
        elif kind == "status_line":
            self.status_var.set(payload[0])
        elif kind == "book_ready":
            book, raw_chunks, chunk_tokens = payload[0]
            self.book = book
            self._populate_chunks(raw_chunks, chunk_tokens)
        elif kind == "meta":
            self._total_chunks, self._total_tokens = payload[0]
            self.progress.config(maximum=max(1, self._total_chunks))
        elif kind == "chunk_started":
            index = payload[0]
            chunk = self.chunks[index - 1]
            chunk.status = "translating"
            self._refresh_chunk_row(chunk)
        elif kind == "progress":
            self._completed_chunks, self._completed_tokens = payload[0]
            self.progress.config(value=self._completed_chunks)
        elif kind == "chunk_result":
            index, translations, error = payload[0]
            chunk = self._apply_chunk_result(index, translations, error)
            if not self.full_translate_active:
                # standalone re-translate -- save immediately, per how this chunk view is meant to work
                if error is not None:
                    messagebox.showwarning(
                        "Re-translate",
                        f"Chunk {chunk.index} still couldn't be translated correctly: {chunk.error}",
                    )
                self._schedule_write()
        elif kind == "write_and_finish":
            total_chunks, error_count = payload[0]
            self._finish_full_translate(total_chunks, error_count)
        elif kind == "write_complete":
            pass
        elif kind == "full_translate_done":
            output_path, error_count, total_chunks = payload[0]
            self.full_translate_active = False
            self._refresh_all_chunk_rows()
            self._update_translate_state()
            elapsed = int(time.time() - self._start_time) if self._start_time else 0
            mins, secs = divmod(elapsed, 60)
            self.status_var.set(f"Done in {mins}:{secs:02d} · {self._total_tokens:,} tokens")
            if error_count:
                messagebox.showinfo(
                    "Translation complete",
                    f"Saved to:\n{output_path}\n\n"
                    f"{error_count}/{total_chunks} chunk(s) kept original text after repeated errors "
                    "-- select them on the left and click Re-translate to try again.",
                )
            else:
                messagebox.showinfo("Translation complete", f"Saved to:\n{output_path}")
        elif kind == "error":
            self.full_translate_active = False
            self._refresh_all_chunk_rows()
            self._update_translate_state()
            self.status_var.set("Failed.")
            messagebox.showerror("Translation failed", payload[0])
        elif kind == "engine_status":
            engine, status, detail = payload[0]
            if engine != self._engine_key():
                return  # a stale check for an engine that's no longer selected -- ignore it
            label = ENGINE_LABELS[engine]
            if status == "ok":
                self._engine_ready = True
                self.engine_status_var.set(f"✓ {label}: logged in as {detail}" if detail else f"✓ {label}: logged in")
                self.login_button.config(text=f"Log in to {label}", state="disabled", bootstyle="secondary-outline")
            elif status == "not_logged_in":
                self._engine_ready = False
                self.engine_status_var.set(f"{label}: not logged in")
                self.login_button.config(text=f"Log in to {label}", state="normal", bootstyle="warning")
            elif status == "not_installed":
                self._engine_ready = False
                self.engine_status_var.set(preflight.ENGINES[engine]["cli_hint"])
                self.login_button.config(text=f"Log in to {label}", state="disabled", bootstyle="secondary-outline")
            self._update_translate_state()


def main():
    _ensure_gui_deps()
    from tkinterdnd2 import TkinterDnD
    import ttkbootstrap as tb

    root = TkinterDnD.Tk()
    tb.Style(theme=THEME)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
