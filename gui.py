#!/usr/bin/env python3
"""Drag-and-drop GUI for translating an EPUB via the Claude Code or Codex CLI.

Bootstraps its own dependencies (tkinterdnd2 for drag-and-drop, tkinterweb for
the rendered preview, plus the epub parsing libraries) the same way
translate_epub.py does, then shows a window: drop or browse for an .epub, pick
an engine and a language, click Load Book. That only parses and chunks the
book -- nothing is translated automatically. Each chunk in the list on the
left gets its own Start button (Retry once it's been attempted) and a Cancel
button that's enabled while it's running and actually kills the in-flight
`claude`/`codex` subprocess; translating whichever chunks you want, in
whatever order, with whichever engine is currently selected, is entirely up
to you. Clicking a chunk renders its current (translated or original) HTML in
a preview pane on the right. The output epub is rewritten after every chunk
that finishes, so it's always in sync with what's been translated so far.

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
import tkinter as tk
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
    ("sv_ttk", "sv-ttk"),
] + preflight.EPUB_DEPS

APP_NAME = "INNO AI Agent Translator"

# Sun Valley ships as the pip package `sv_ttk` (a plain ttk theme, applied via
# sv_ttk.set_theme). Forest isn't on PyPI, so its .tcl + image assets are vendored
# under themes/forest/ (from https://github.com/rdbende/Forest-ttk-theme, MIT) and
# sourced directly with `root.tk.call("source", ...)`.
FOREST_DIR = Path(__file__).resolve().parent / "themes" / "forest"

THEME_OPTIONS = ["Sun Valley Light", "Sun Valley Dark", "Forest Light", "Forest Dark"]
THEME_ENGINE = {
    "Sun Valley Light": ("sv", "light"),
    "Sun Valley Dark": ("sv", "dark"),
    "Forest Light": ("forest", "light"),
    "Forest Dark": ("forest", "dark"),
}
DEFAULT_THEME = "Sun Valley Light"

# Raw (non-ttk) widgets -- the drop zone, the chunk-list canvas, the empty preview
# page -- don't follow ttk theme switches automatically, so their colors are looked
# up here per (engine, mode) instead of read off a live Style object. Matches each
# theme's own background/foreground/accent so they blend in rather than clash.
PALETTES = {
    ("sv", "light"): dict(bg="#fafafa", fg="#1c1c1c", secondary="#6c757d", inputbg="#f0f0f0", border="#d5d5d5"),
    ("sv", "dark"): dict(bg="#1c1c1c", fg="#fafafa", secondary="#9aa0a6", inputbg="#282828", border="#3a3a3a"),
    ("forest", "light"): dict(bg="#ffffff", fg="#313131", secondary="#6c757d", inputbg="#f2f2f2", border="#d5d5d5"),
    ("forest", "dark"): dict(bg="#313131", fg="#eeeeee", secondary="#a9a9a9", inputbg="#3a3a3a", border="#4a4a4a"),
}

# Status/semantic colors don't need to vary by engine, just by light/dark -- applied
# as plain foreground tints (via custom-named ttk styles, e.g. "Done.TLabel") rather
# than the colored "pill" badges ttkbootstrap did, since neither Sun Valley nor
# Forest draw colored-background labels/buttons out of the box.
STATUS_COLORS = {
    "light": dict(queued="#6c757d", translating="#b8860b", done="#1e7d34", failed="#c0392b", cancelled="#6c757d"),
    "dark": dict(queued="#9aa0a6", translating="#e0a63a", done="#66bb6a", failed="#e57373", cancelled="#9aa0a6"),
}

# claude/codex ride an existing CLI login (checked via preflight.ENGINES); local_ai has no
# such thing -- it's a plain HTTP client against a URL/key the user configures themselves,
# so it's handled separately wherever engine-specific setup/readiness logic comes up.
DRIVERS = {"claude": claude_driver, "codex": codex_driver, "local_ai": local_ai_driver}
ENGINE_KEYS = ["claude", "codex", "local_ai"]
ENGINE_LABELS = {**{key: preflight.ENGINES[key]["label"] for key in ("claude", "codex")}, "local_ai": "Local AI"}
ENGINE_KEYS_BY_LABEL = {label: key for key, label in ENGINE_LABELS.items()}

COMMON_LANGUAGES = [
    "English", "Spanish", "French", "German", "Italian", "Portuguese (Brazil)",
    "Japanese", "Korean", "Simplified Chinese", "Arabic", "Russian", "Hindi",
]

MAX_TOKENS_PER_CHUNK = 3000

STATUS_LABELS = {
    "queued": ("queued", "Queued"),
    "translating": ("translating…", "Translating"),
    "done": ("done", "Done"),
    "failed": ("failed", "Failed"),
    "cancelled": ("cancelled", "Cancelled"),
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
        self.status = "queued"  # queued | translating | done | failed | cancelled
        self.error = None
        self.cancel_event = None
        self.row_frame = None
        self.info_label = None
        self.status_label = None
        self.start_button = None
        self.cancel_button = None
        self.progress_bar = None

    def current_html(self) -> str:
        return "\n".join(str(u.tag) for u in self.units)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)

        self.source_path = None
        self.msg_queue = queue.Queue()
        self._loading = False  # parsing/chunking the book -- not translation, which is per-chunk now
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

        self._forest_loaded = set()  # which forest-<mode> .tcl files have been sourced already
        self.theme_var = tk.StringVar(value=DEFAULT_THEME)
        self._current_palette = self._apply_ttk_theme(*THEME_ENGINE[DEFAULT_THEME])

        self._build_ui()
        self.theme_var.trace_add("write", lambda *_: self._on_theme_changed())

        # Size the window to what its content actually needs rather than a fixed
        # guess -- these two themes' own paddings run taller than ttkbootstrap's did,
        # so a hardcoded geometry was clipping the bottom of the sidebar (Load Book,
        # the progress bar) with no way to scroll down to it. minsize matches, so
        # shrinking the window can't reintroduce that clipping; it can still grow.
        # A full update() (not just update_idletasks()) so widgets are actually
        # mapped before measuring -- some font/image metrics these two themes use
        # only settle once realized on screen, and measuring too early under-reports
        # by a few pixels, clipping the last row again. The small manual buffer on
        # top covers whatever's still left after that.
        self.root.update()
        req_w, req_h = self.root.winfo_reqwidth(), self.root.winfo_reqheight() + 36
        self.root.geometry(f"{req_w}x{req_h}")
        self.root.minsize(req_w, req_h)

        self._poll_queue()
        self._check_engine_async(self._engine_key())

    # ---- theme (Sun Valley / Forest) ----

    def _load_forest_theme(self, mode: str):
        if mode not in self._forest_loaded:
            self.root.tk.call("source", str(FOREST_DIR / f"forest-{mode}.tcl"))
            self._forest_loaded.add(mode)

    def _apply_ttk_theme(self, family: str, mode: str) -> dict:
        """Switches the active ttk theme, then (re)defines the small set of
        semantic style names (Danger.TButton, DoneStatus.TLabel, etc.) this app
        relies on -- ttk keeps style configuration per-theme, so it has to be
        redone after every theme_use()/set_theme() call, not just once at
        startup. Returns the raw-widget color palette for this theme."""
        style = ttk.Style(self.root)
        if family == "sv":
            import sv_ttk
            sv_ttk.set_theme(mode, root=self.root)
        else:
            self._load_forest_theme(mode)
            style.theme_use(f"forest-{mode}")

        status_colors = STATUS_COLORS[mode]
        style.configure("Danger.TButton", foreground=status_colors["failed"])
        for name, key in (("Queued", "queued"), ("Translating", "translating"), ("Done", "done"),
                          ("Failed", "failed"), ("Cancelled", "cancelled")):
            style.configure(f"{name}Status.TLabel", foreground=status_colors[key])

        # Forest's own disabled-button foreground is the exact same gray as its
        # disabled-button background in dark mode (#595959 on #595959) -- invisible
        # text. Override with our own disabled tint on every theme so a disabled
        # button never goes illegible, regardless of what the theme itself ships.
        disabled_fg = "#707070" if mode == "light" else "#c8c8c8"
        style.map("TButton", foreground=[("disabled", disabled_fg)])
        style.map("Danger.TButton", foreground=[("disabled", disabled_fg)])

        pal = PALETTES[(family, mode)]
        style.configure("Muted.TLabel", foreground=pal["secondary"])
        self._family, self._mode = family, mode
        return pal

    def _on_theme_changed(self):
        family, mode = THEME_ENGINE[self.theme_var.get()]
        pal = self._apply_ttk_theme(family, mode)
        self._current_palette = pal
        self.drop_frame.config(bg=pal["inputbg"], highlightbackground=pal["border"])
        self.drop_label.config(bg=pal["inputbg"], fg=pal["secondary"])
        self.chunks_canvas.config(bg=pal["bg"])
        self.preview_container.config(highlightbackground=pal["border"])
        self._preview_bg = pal["bg"]
        if self.selected_chunk is None:
            self._render_preview_empty()

    # ---- UI construction ----

    def _build_ui(self):
        pal = self._current_palette

        # Two-column layout: a fixed-width sidebar on the left holds the setup controls
        # (engine, login, from/to language, save-as) -- none of that needs to stretch
        # across the whole window, it was just filling the space because it was the only
        # thing in a full-width row. The chunk list + preview -- the "chapters and
        # details" that actually grow with the book -- get the rest of the window on
        # the right, full height, instead of being squeezed into a strip at the bottom.
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True)

        # No fixed width/pack_propagate(False) here -- the sidebar sizes itself to
        # what its content actually needs (a Labelframe with a combobox, an entry,
        # etc. all naturally want roughly the same width), so nothing inside it
        # ever gets clipped by a container that was guessed too small.
        sidebar = ttk.Frame(main, padding=(16, 14, 10, 14))
        sidebar.pack(side="left", fill="y")

        content = ttk.Frame(main, padding=(6, 14, 14, 14))
        content.pack(side="left", fill="both", expand=True)

        header_row = ttk.Frame(sidebar)
        header_row.pack(fill="x")
        ttk.Label(header_row, text=APP_NAME, font=("", 16, "bold"),
                  wraplength=230, justify="left").pack(side="left", anchor="n")
        self.settings_button = ttk.Button(header_row, text="⚙", width=3, command=self._open_settings_dialog)
        self.settings_button.pack(side="right", anchor="n")

        ttk.Label(sidebar, text="Claude, Codex, or a local model -- formatting intact.",
                  style="Muted.TLabel", wraplength=280, justify="left").pack(anchor="w", pady=(2, 10))

        # Usage card: Claude Code's plan-usage percentages (session/week), pulled via
        # `claude -p "/usage"` since there's no JSON API for it -- see
        # preflight.claude_usage_status. Only meaningful for the claude engine.
        usage = ttk.Labelframe(sidebar, text="Usage", padding=10)
        usage.pack(fill="x", pady=(0, 10))
        usage.columnconfigure(1, weight=1)

        ttk.Label(usage, text="Session").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.session_usage_bar = ttk.Progressbar(usage, mode="determinate", maximum=100)
        self.session_usage_bar.grid(row=0, column=1, sticky="ew")
        self.session_usage_label = ttk.Label(usage, text="--", width=5, anchor="e")
        self.session_usage_label.grid(row=0, column=2, padx=(6, 0))

        ttk.Label(usage, text="Week").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(5, 0))
        self.week_usage_bar = ttk.Progressbar(usage, mode="determinate", maximum=100)
        self.week_usage_bar.grid(row=1, column=1, sticky="ew", pady=(5, 0))
        self.week_usage_label = ttk.Label(usage, text="--", width=5, anchor="e")
        self.week_usage_label.grid(row=1, column=2, padx=(6, 0), pady=(5, 0))

        self.usage_reset_var = tk.StringVar(value="")
        ttk.Label(usage, textvariable=self.usage_reset_var, style="Muted.TLabel",
                  wraplength=250, justify="left").grid(row=2, column=0, columnspan=3, sticky="w", pady=(5, 0))

        self.usage_refresh_button = ttk.Button(usage, text="Refresh", width=8,
                                                command=self._refresh_usage_async)
        self.usage_refresh_button.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Drop zone -- uses inputbg/border rather than bg/fg: those two are reliably
        # distinct from the page background in both light and dark themes.
        self.drop_frame = tk.Frame(sidebar, bg=pal["inputbg"], highlightbackground=pal["border"],
                                    highlightthickness=1, height=76)
        self.drop_frame.pack(fill="x", pady=6)
        self.drop_label = tk.Label(self.drop_frame, text="Drag an .epub file here, or click to browse",
                                    bg=pal["inputbg"], fg=pal["secondary"], font=("", 11), cursor="hand2",
                                    wraplength=260, justify="center")
        self.drop_label.pack(expand=True, fill="both", pady=10)
        self.drop_label.bind("<Button-1>", lambda e: self._browse())
        self.drop_frame.bind("<Button-1>", lambda e: self._browse())

        try:
            from tkinterdnd2 import DND_FILES
            for widget in (self.drop_label, self.drop_frame):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass  # drag-and-drop unavailable; clicking to browse still works

        self.translate_button = ttk.Button(sidebar, text="Load Book", style="TButton",
                                            command=self._on_load_book, state="disabled")
        self.translate_button.pack(fill="x", pady=(4, 8))

        self.progress = ttk.Progressbar(sidebar, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 4))

        self.status_var = tk.StringVar(value="")
        ttk.Label(sidebar, textvariable=self.status_var, style="Muted.TLabel",
                  wraplength=280, justify="left").pack(anchor="w", fill="x", pady=(0, 4))

        # Chunk list (left) + rendered preview (right) -- "chapters and details" for
        # the loaded book, filling the entire right side of the window.
        paned = ttk.PanedWindow(content, orient="horizontal")
        paned.pack(fill="both", expand=True)

        chunks_container = ttk.Frame(paned)
        paned.add(chunks_container, weight=2)

        # ttk.Panedwindow sizes each pane from its requested/natural size on first
        # layout (weight= only governs how *extra* space is redistributed on later
        # resizes), and a bare Canvas has no natural width of its own -- without an
        # explicit width hint here the initial split can leave this pane too narrow
        # to fit a row's status badge and Retry button.
        self.chunks_canvas = tk.Canvas(chunks_container, highlightthickness=0, bg=pal["bg"], width=420)
        chunks_scrollbar = ttk.Scrollbar(chunks_container, orient="vertical", command=self.chunks_canvas.yview)
        self.chunks_inner = ttk.Frame(self.chunks_canvas)
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

        self.chunks_placeholder = ttk.Label(
            self.chunks_inner, text="Chunks will appear here once you click Translate.",
            style="Muted.TLabel", wraplength=260, justify="left",
        )
        self.chunks_placeholder.pack(padx=8, pady=8, anchor="w")

        # A visible border around the preview frame: the HTML preview stays a plain
        # white/light "page" regardless of app theme (book content reads best on a
        # neutral light background), so it needs a clear frame around it in a dark
        # theme -- otherwise it looks like a stray rendering glitch instead of a
        # deliberate reading pane.
        self.preview_container = tk.Frame(paned, highlightbackground=pal["border"], highlightthickness=1)
        paned.add(self.preview_container, weight=2)

        # Prev/Next bar above the preview -- lets you page through chunks without
        # hunting for them in the (potentially long) list on the left.
        preview_nav = ttk.Frame(self.preview_container, padding=(8, 6))
        preview_nav.pack(side="top", fill="x")

        self.preview_prev_button = ttk.Button(preview_nav, text="◀ Previous", width=12,
                                               command=lambda: self._navigate_chunk(-1),
                                               state="disabled")
        self.preview_prev_button.pack(side="left")

        self.preview_chunk_label = ttk.Label(preview_nav, text="", style="Muted.TLabel", anchor="center")
        self.preview_chunk_label.pack(side="left", fill="x", expand=True)

        self.preview_next_button = ttk.Button(preview_nav, text="Next ▶", width=12,
                                               command=lambda: self._navigate_chunk(1),
                                               state="disabled")
        self.preview_next_button.pack(side="right")

        from tkinterweb import HtmlFrame
        # width/height are just initial-layout hints (pack(fill="both", expand=True)
        # below still lets it grow to whatever the pane actually gets) -- tkinterweb's
        # default requested size is ~800x600, which is bigger than the chunk list's,
        # so with equal pane weights below, ttk.Panedwindow's deficit-splitting (equal
        # weight = equal absolute pixels trimmed from each pane's *requested* size, not
        # proportional) crushed the narrower chunk-list pane down to ~140px before this.
        self.preview_frame = HtmlFrame(self.preview_container, messages_enabled=False, width=300, height=200)
        self.preview_frame.pack(fill="both", expand=True)
        # Only tinted while empty -- once there's real translated content to show,
        # it switches to a plain light page (book text reads best that way), but an
        # unused panel showing stark white next to a dark theme looks like a bug.
        self._preview_bg = pal["bg"]
        self._render_preview_empty()

        self._build_settings_dialog()

    def _build_settings_dialog(self):
        """Theme + Setup (engine/login/languages/output name) live in their own
        dialog, opened via the ⚙ button, rather than eating sidebar space that's
        needed once a book is loaded. Built once and hidden (not destroyed) on
        close, since background threads keep updating engine_status_label/
        login_button/etc. regardless of whether the dialog is currently shown."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Settings")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", dialog.withdraw)
        dialog.withdraw()  # built up front so its widgets exist, but hidden until opened

        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)

        theme_row = ttk.Frame(body)
        theme_row.pack(fill="x", pady=(0, 10))
        ttk.Label(theme_row, text="Theme").pack(side="left")
        self.theme_combo = ttk.Combobox(theme_row, textvariable=self.theme_var, state="readonly",
                                         values=THEME_OPTIONS, width=15)
        self.theme_combo.pack(side="right")

        setup = ttk.Labelframe(body, text="Setup", padding=10)
        setup.pack(fill="x")
        setup.columnconfigure(1, weight=1)

        ttk.Label(setup, text="Engine").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 6))
        self.engine_var = tk.StringVar(value=ENGINE_LABELS["claude"])
        self.engine_combo = ttk.Combobox(setup, textvariable=self.engine_var, state="readonly",
                                          values=[ENGINE_LABELS[k] for k in ENGINE_KEYS])
        self.engine_combo.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        self.engine_var.trace_add("write", lambda *_: self._on_engine_changed())

        self.engine_settings_button = ttk.Button(setup, text="⚙", width=3,
                                                  command=self._open_local_ai_settings)
        self.engine_settings_button.grid(row=0, column=2, padx=(6, 0), pady=(0, 6))
        self.engine_settings_button.grid_remove()

        self.engine_status_var = tk.StringVar(value="Checking...")
        self.engine_status_label = ttk.Label(setup, textvariable=self.engine_status_var,
                                              style="Muted.TLabel", wraplength=260, justify="left")
        self.engine_status_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self.login_button = ttk.Button(setup, text="Log in", command=self._start_login, state="disabled")
        self.login_button.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 4))

        ttk.Separator(setup).grid(row=3, column=0, columnspan=3, sticky="ew", pady=6)

        ttk.Label(setup, text="From").grid(row=4, column=0, sticky="w", padx=(0, 10), pady=(0, 6))
        self.source_lang_var = tk.StringVar(value=translation_common.AUTO_DETECT)
        self.source_lang_combo = ttk.Combobox(setup, textvariable=self.source_lang_var,
                                               values=[translation_common.AUTO_DETECT] + COMMON_LANGUAGES)
        self.source_lang_combo.grid(row=4, column=1, sticky="ew", pady=(0, 6))

        ttk.Label(setup, text="To").grid(row=5, column=0, sticky="w", padx=(0, 10), pady=(0, 6))
        self.lang_var = tk.StringVar()
        self.lang_combo = ttk.Combobox(setup, textvariable=self.lang_var, values=COMMON_LANGUAGES)
        self.lang_combo.grid(row=5, column=1, sticky="ew", pady=(0, 6))
        self.lang_var.trace_add("write", lambda *_: self._on_language_changed())

        ttk.Label(setup, text="Save as").grid(row=6, column=0, sticky="w", padx=(0, 10))
        self.output_name_var = tk.StringVar()
        self._last_auto_name = ""
        self.output_name_entry = ttk.Entry(setup, textvariable=self.output_name_var)
        self.output_name_entry.grid(row=6, column=1, sticky="ew")

        ttk.Button(body, text="Close", command=dialog.withdraw).pack(anchor="e", pady=(12, 0))

        self.settings_dialog = dialog

    def _open_settings_dialog(self):
        self.settings_dialog.deiconify()
        self.settings_dialog.lift()
        self.settings_dialog.focus_force()

    def _bind_scroll(self, widget):
        """Binding the wheel directly on `widget` only catches events when the
        pointer is over that exact widget -- Tk doesn't bubble MouseWheel from
        a child up to its parent, so hovering over any row's label/button/progress
        bar (all children of the canvas's embedded frame) would do nothing. Instead
        bind globally (low priority, fires after any widget-specific handling like
        tkinterweb's own) and only act when the widget under the pointer is `widget`
        or one of its descendants."""
        def on_wheel(event):
            node = self.root.winfo_containing(event.x_root, event.y_root)
            while node is not None and node is not widget:
                node = node.master
            if node is None:
                return
            if getattr(event, "num", None) == 4:
                widget.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                widget.yview_scroll(1, "units")
            else:
                widget.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.root.bind_all("<MouseWheel>", on_wheel, add="+")
        self.root.bind_all("<Button-4>", on_wheel, add="+")
        self.root.bind_all("<Button-5>", on_wheel, add="+")

    # ---- file selection ----

    def _any_chunk_busy(self) -> bool:
        return any(c.status == "translating" for c in self.chunks)

    def _browse(self):
        if self._loading or self._any_chunk_busy():
            return
        path = filedialog.askopenfilename(title="Choose an EPUB", filetypes=[("EPUB files", "*.epub")])
        if path:
            self._set_file(Path(path))

    def _on_drop(self, event):
        if self._loading or self._any_chunk_busy():
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

        if engine == "claude":
            self.usage_refresh_button.config(state="normal")
        else:
            self.usage_refresh_button.config(state="disabled")
            self.session_usage_bar.config(value=0)
            self.week_usage_bar.config(value=0)
            self.session_usage_label.config(text="--")
            self.week_usage_label.config(text="--")
            self.usage_reset_var.set("Usage stats are only available for the Claude Code engine.")

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

    def _refresh_usage_async(self):
        if self._engine_key() != "claude":
            return
        self.usage_refresh_button.config(state="disabled")
        self.usage_reset_var.set("Checking...")

        def work():
            status = preflight.claude_usage_status()
            self.msg_queue.put(("usage_status", status))

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
        dialog = tk.Toplevel(self.root)
        dialog.title("Local AI settings")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        form = ttk.Frame(dialog, padding=16)
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, weight=1, minsize=280)

        ttk.Label(form, text="Base URL").grid(row=0, column=0, sticky="w", pady=(0, 8), padx=(0, 12))
        base_url_var = tk.StringVar(value=self.local_ai_base_url)
        ttk.Entry(form, textvariable=base_url_var).grid(row=0, column=1, sticky="ew", pady=(0, 8))

        ttk.Label(form, text="API Key").grid(row=1, column=0, sticky="w", pady=(0, 8), padx=(0, 12))
        api_key_var = tk.StringVar(value=self.local_ai_api_key)
        ttk.Entry(form, textvariable=api_key_var, show="*").grid(row=1, column=1, sticky="ew", pady=(0, 8))

        ttk.Label(form, text="Model").grid(row=2, column=0, sticky="w", pady=(0, 8), padx=(0, 12))
        model_var = tk.StringVar(value=self.local_ai_model)
        model_combo = ttk.Combobox(form, textvariable=model_var, values=[])
        model_combo.grid(row=2, column=1, sticky="ew", pady=(0, 8))

        test_status_var = tk.StringVar(value="")
        ttk.Label(form, textvariable=test_status_var, style="Muted.TLabel",
                  wraplength=360, justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 8))

        button_row = ttk.Frame(form)
        button_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        test_button = ttk.Button(button_row, text="Test connection")
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

        ttk.Button(button_row, text="Save", style="Accent.TButton", command=do_save).pack(side="right")
        ttk.Button(button_row, text="Cancel", command=dialog.destroy).pack(side="right", padx=(0, 8))

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
        self._update_preview_nav()

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
        row = ttk.Frame(self.chunks_inner, padding=(8, 6))
        row.pack(fill="x", padx=2, pady=1)

        # header is its own full-width frame (rather than packing the label/status/button
        # straight into row) so the progress bar below has a "cavity" to fill the whole
        # row's width -- packing side="left" widgets leaves only a slim strip next to them.
        header = ttk.Frame(row)
        header.pack(fill="x")

        info_label = ttk.Label(header, text=f"Chunk {chunk.index}   ·   ~{chunk.tokens:,} tok",
                                cursor="hand2", anchor="w")
        info_label.pack(side="left", fill="x", expand=True)
        info_label.bind("<Button-1>", lambda e, c=chunk: self._select_chunk(c))

        status_label = ttk.Label(header, text="queued", style="QueuedStatus.TLabel",
                                  width=13, anchor="center")
        status_label.pack(side="left", padx=(4, 8))

        start_button = ttk.Button(header, text="Start", width=7,
                                   command=lambda c=chunk: self._on_start_chunk(c))
        start_button.pack(side="left", padx=(4, 0))

        cancel_button = ttk.Button(header, text="Cancel", style="Danger.TButton", width=7,
                                    command=lambda c=chunk: self._on_cancel_chunk(c), state="disabled")
        cancel_button.pack(side="left", padx=(4, 0))

        progress_bar = ttk.Progressbar(row, mode="indeterminate")

        chunk.row_frame = row
        chunk.info_label = info_label
        chunk.status_label = status_label
        chunk.start_button = start_button
        chunk.cancel_button = cancel_button
        chunk.progress_bar = progress_bar

    def _refresh_chunk_row(self, chunk: "ChunkInfo"):
        text, style_key = STATUS_LABELS.get(chunk.status, (chunk.status, "Queued"))
        translating = chunk.status == "translating"
        if chunk.status_label is not None:
            chunk.status_label.config(text=text, style=f"{style_key}Status.TLabel")
        if chunk.start_button is not None:
            label = "Start" if chunk.status == "queued" else "Retry"
            busy = translating or self._loading
            chunk.start_button.config(text=label, state="disabled" if busy else "normal")
        if chunk.cancel_button is not None:
            chunk.cancel_button.config(state="normal" if translating else "disabled")
        if chunk.progress_bar is not None:
            if translating:
                if not chunk.progress_bar.winfo_ismapped():
                    chunk.progress_bar.pack(fill="x", pady=(4, 0))
                    chunk.progress_bar.start(12)
            elif chunk.progress_bar.winfo_ismapped():
                chunk.progress_bar.stop()
                chunk.progress_bar.pack_forget()

    def _select_chunk(self, chunk: "ChunkInfo"):
        if self.selected_chunk is not None and self.selected_chunk.info_label is not None:
            self.selected_chunk.info_label.config(font=("", 10, "normal"))
        self.selected_chunk = chunk
        if chunk.info_label is not None:
            chunk.info_label.config(font=("", 10, "bold"))
        self._render_chunk_preview(chunk)
        self._update_preview_nav()

    def _navigate_chunk(self, delta: int):
        if not self.chunks:
            return
        if self.selected_chunk is None:
            new_index = 0 if delta > 0 else len(self.chunks) - 1
        else:
            new_index = self.chunks.index(self.selected_chunk) + delta
        if 0 <= new_index < len(self.chunks):
            self._select_chunk(self.chunks[new_index])

    def _update_preview_nav(self):
        total = len(self.chunks)
        if self.selected_chunk is None or total == 0:
            self.preview_chunk_label.config(text="")
            self.preview_prev_button.config(state="disabled")
            self.preview_next_button.config(state="disabled")
            return
        index = self.chunks.index(self.selected_chunk)
        self.preview_chunk_label.config(text=f"Chunk {index + 1} of {total}")
        self.preview_prev_button.config(state="normal" if index > 0 else "disabled")
        self.preview_next_button.config(state="normal" if index < total - 1 else "disabled")

    def _render_chunk_preview(self, chunk: "ChunkInfo"):
        self._render_preview_html(f"<html><body>{chunk.current_html()}</body></html>")

    def _render_preview_empty(self):
        self._render_preview_html(f'<html><body style="background:{self._preview_bg};"></body></html>')

    def _render_preview_html(self, html: str):
        try:
            self.preview_frame.load_html(html)
        except Exception:
            pass

    # ---- load book (parse + chunk only -- translating each chunk is manual, see below) ----

    def _update_translate_state(self):
        ready = (
            self.source_path is not None
            and self.lang_var.get().strip() != ""
            and not self._loading
            and not self._any_chunk_busy()
            and self._engine_ready
        )
        # Forest's Accent.TButton doesn't dim much when disabled, so the style itself
        # (not just the state) has to carry "not ready yet" -- plain button while
        # disabled, accent only once it's actually clickable.
        self.translate_button.config(state="normal" if ready else "disabled",
                                      style="Accent.TButton" if ready else "TButton")

    def _on_load_book(self):
        if self._loading or self._any_chunk_busy() or self.source_path is None:
            return
        target_lang = self.lang_var.get().strip()
        if not target_lang:
            return
        source_lang = self.source_lang_var.get().strip()

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
        self.output_path = output_path
        self._loading = True
        self.translate_button.config(state="disabled")
        self.progress.config(value=0, maximum=1)
        self.status_var.set(f"Parsing {self.source_path.name}...")
        self._clear_chunks()

        threading.Thread(target=self._parse_and_load, args=(self.source_path,), daemon=True).start()

    def _engine_call_kwargs(self, engine: str) -> dict:
        """Extra keyword args a driver's translate_chunk needs beyond
        (units, target_lang, model) -- only local_ai needs anything here."""
        if engine == "local_ai":
            return {"base_url": self.local_ai_base_url, "api_key": self.local_ai_api_key}
        return {}

    def _engine_model(self, engine: str):
        return self.local_ai_model if engine == "local_ai" else None

    def _update_overall_progress(self):
        total = len(self.chunks)
        finished = sum(1 for c in self.chunks if c.status in ("done", "failed", "cancelled"))
        self.progress.config(value=finished, maximum=max(1, total))
        if total and finished:
            self.status_var.set(f"{finished}/{total} chunk(s) finished.")

    def _parse_and_load(self, source_path):
        """Parse the epub and pack it into chunks -- nothing is translated here.
        Each chunk shows up in the list with a Start button; translating it is
        entirely up to the user clicking that button (or Retry, once attempted)."""
        try:
            import epub_io

            book = epub_io.load_book(str(source_path))
            chunks = translation_common.pack_chunks(book.units, MAX_TOKENS_PER_CHUNK)
            chunk_tokens = [sum(translation_common.estimate_tokens(u.html) for u in chunk) for chunk in chunks]
            total_tokens = sum(chunk_tokens)
            pages = epub_io.estimate_pages(book.units)

            self.msg_queue.put(("book_ready", (book, chunks, chunk_tokens)))
            self.msg_queue.put(("status_line", (
                f"{len(book.chapters)} chapter(s), ~{pages} page(s), {len(book.units)} unit(s), "
                f"~{total_tokens:,} estimated tokens, packed into {len(chunks)} chunk(s). "
                "Click Start on a chunk to translate it."
            )))
        except Exception as exc:
            self.msg_queue.put(("error", str(exc)))

    # ---- per-chunk translate (Start / Retry / Cancel) ----

    def _on_start_chunk(self, chunk: "ChunkInfo"):
        if chunk.status == "translating" or self._loading:
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

        chunk.status = "translating"
        chunk.cancel_event = threading.Event()
        cancel_event = chunk.cancel_event
        self._refresh_chunk_row(chunk)
        self._update_translate_state()
        model = self._engine_model(engine)
        engine_kwargs = self._engine_call_kwargs(engine)

        def work():
            driver = DRIVERS[engine]
            translations, error = driver.translate_chunk(
                chunk.units, target_lang, model, source_lang=source_lang,
                cancel_event=cancel_event, **engine_kwargs
            )
            self.msg_queue.put(("chunk_result", (chunk.index, translations, error)))

        threading.Thread(target=work, daemon=True).start()

    def _on_cancel_chunk(self, chunk: "ChunkInfo"):
        if chunk.status != "translating" or chunk.cancel_event is None:
            return
        chunk.cancel_event.set()
        chunk.cancel_button.config(state="disabled")  # avoid double-clicks while the worker winds down

    def _apply_chunk_result(self, index, translations, error) -> "ChunkInfo":
        chunk = self.chunks[index - 1]
        import epub_io
        try:
            epub_io.apply_translations(chunk.units, translations)
        except Exception as exc:
            error = error or exc
        cancelled = isinstance(error, translation_common.TranslationCancelled)
        chunk.status = "cancelled" if cancelled else ("failed" if error else "done")
        chunk.error = error
        chunk.cancel_event = None
        self._refresh_chunk_row(chunk)
        if self.selected_chunk is chunk:
            self._render_chunk_preview(chunk)
        self._update_translate_state()
        self._update_overall_progress()
        if error is not None and not cancelled:
            messagebox.showwarning(
                "Translate",
                f"Chunk {chunk.index} still has errors (some text may have fallen back "
                f"to the original): {chunk.error}",
            )
        self._schedule_write()
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
            self._loading = False
            self._update_translate_state()
            self._update_overall_progress()
        elif kind == "chunk_result":
            index, translations, error = payload[0]
            self._apply_chunk_result(index, translations, error)
        elif kind == "write_complete":
            pass
        elif kind == "usage_status":
            status = payload[0]
            if self._engine_key() != "claude":
                return  # stale check for an engine that's no longer selected
            self.usage_refresh_button.config(state="normal")
            session_pct, week_pct = status.get("session_pct"), status.get("week_pct")
            self.session_usage_bar.config(value=session_pct or 0)
            self.session_usage_label.config(text=f"{session_pct}%" if session_pct is not None else "--")
            self.week_usage_bar.config(value=week_pct or 0)
            self.week_usage_label.config(text=f"{week_pct}%" if week_pct is not None else "--")
            resets = [f"{label} resets {status[key]}" for label, key in
                      (("Session", "session_reset"), ("Week", "week_reset")) if status.get(key)]
            self.usage_reset_var.set(" · ".join(resets) if resets else "Couldn't read usage.")
        elif kind == "error":
            self._loading = False
            self._update_translate_state()
            self.status_var.set("Failed to load the book.")
            messagebox.showerror("Load failed", payload[0])
        elif kind == "engine_status":
            engine, status, detail = payload[0]
            if engine != self._engine_key():
                return  # a stale check for an engine that's no longer selected -- ignore it
            label = ENGINE_LABELS[engine]
            if status == "ok":
                self._engine_ready = True
                self.engine_status_var.set(f"✓ {label}: logged in as {detail}" if detail else f"✓ {label}: logged in")
                self.login_button.config(text=f"Log in to {label}", state="disabled", style="TButton")
                if engine == "claude":
                    self._refresh_usage_async()
            elif status == "not_logged_in":
                self._engine_ready = False
                self.engine_status_var.set(f"{label}: not logged in")
                self.login_button.config(text=f"Log in to {label}", state="normal", style="Accent.TButton")
            elif status == "not_installed":
                self._engine_ready = False
                self.engine_status_var.set(preflight.ENGINES[engine]["cli_hint"])
                self.login_button.config(text=f"Log in to {label}", state="disabled", style="TButton")
            self._update_translate_state()


def main():
    _ensure_gui_deps()
    from tkinterdnd2 import TkinterDnD

    root = TkinterDnD.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
