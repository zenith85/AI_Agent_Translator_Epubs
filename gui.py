#!/usr/bin/env python3
"""Drag-and-drop GUI for translating an EPUB via the Claude Code CLI.

Bootstraps its own dependencies (tkinterdnd2 for drag-and-drop, tkinterweb for
the rendered preview, plus the epub parsing libraries) the same way
translate_epub.py does, then shows a window: drop or browse for an .epub,
pick a language, click Translate. Once translated, every chunk shows up in a
list on the left with its own Re-translate button; clicking a chunk renders
its current (translated) HTML in a preview pane on the right.

Threading model: worker threads only ever call claude_driver (network-bound,
side-effect-free) and hand results back over a queue. All BeautiffulSoup tree
mutation (epub_io.apply_translations) and epub writing happen on the main
thread (in response to queued messages), so nothing races with the preview
pane reading the same tree."""

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

import preflight  # stdlib-only, safe to import before dependency checks

GUI_DEPS = [("tkinterdnd2", "tkinterdnd2"), ("tkinterweb", "tkinterweb")] + preflight.EPUB_DEPS

COMMON_LANGUAGES = [
    "Spanish", "French", "German", "Italian", "Portuguese (Brazil)",
    "Japanese", "Korean", "Simplified Chinese", "Arabic", "Russian", "Hindi",
]

MAX_TOKENS_PER_CHUNK = 6000
CONCURRENCY = 3

STATUS_LABELS = {
    "queued": ("queued", "#999999"),
    "translating": ("translating…", "#b8860b"),
    "done": ("done", "#2a7a2a"),
    "failed": ("failed", "#b3261e"),
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
        self.root.title("EPUB Translator")
        self.root.geometry("960x640")
        self.root.minsize(800, 520)

        self.source_path = None
        self.msg_queue = queue.Queue()
        self.full_translate_active = False
        self._claude_ready = False

        self.book = None
        self.chunks = []  # list[ChunkInfo]
        self.selected_chunk = None
        self.output_path = None
        self.target_lang = ""
        self._write_lock = threading.Lock()

        self._start_time = None
        self._total_tokens = 0
        self._completed_tokens = 0
        self._total_chunks = 0
        self._completed_chunks = 0

        self._build_ui()
        self._poll_queue()
        self._check_claude_async()

    # ---- UI construction ----

    def _build_ui(self):
        pad = {"padx": 16, "pady": 8}

        ttk.Label(self.root, text="EPUB Translator", font=("", 16, "bold")).pack(pady=(16, 0))
        ttk.Label(self.root, text="Translate a book with Claude, formatting intact.",
                  foreground="#666").pack(pady=(0, 8))

        self.claude_status_var = tk.StringVar(value="Checking Claude Code...")
        self.claude_status_label = ttk.Label(self.root, textvariable=self.claude_status_var,
                                              foreground="#666", wraplength=880, justify="left")
        self.claude_status_label.pack(padx=16, pady=(0, 4), fill="x")

        self.login_button = ttk.Button(self.root, text="Log in to Claude Code", command=self._start_login)

        # Drop zone
        self.drop_frame = tk.Frame(self.root, bg="#f4f4f6", highlightbackground="#bbbbbb",
                                    highlightthickness=2, height=90)
        self.drop_frame.pack(fill="x", padx=16, pady=8)
        self.drop_label = tk.Label(self.drop_frame, text="Drag an .epub file here\nor click to browse",
                                    bg="#f4f4f6", fg="#555555", cursor="hand2")
        self.drop_label.pack(expand=True, fill="both", pady=20)
        self.drop_label.bind("<Button-1>", lambda e: self._browse())
        self.drop_frame.bind("<Button-1>", lambda e: self._browse())

        try:
            from tkinterdnd2 import DND_FILES
            for widget in (self.drop_label, self.drop_frame):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass  # drag-and-drop unavailable; clicking to browse still works

        # Language picker
        lang_row = ttk.Frame(self.root)
        lang_row.pack(fill="x", **pad)
        ttk.Label(lang_row, text="Translate to:").pack(side="left")
        self.lang_var = tk.StringVar()
        self.lang_combo = ttk.Combobox(lang_row, textvariable=self.lang_var, values=COMMON_LANGUAGES)
        self.lang_combo.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.lang_var.trace_add("write", lambda *_: self._on_language_changed())

        # Output filename -- pre-filled with a suggested name, freely editable
        name_row = ttk.Frame(self.root)
        name_row.pack(fill="x", **pad)
        ttk.Label(name_row, text="Save as:").pack(side="left")
        self.output_name_var = tk.StringVar()
        self._last_auto_name = ""
        self.output_name_entry = ttk.Entry(name_row, textvariable=self.output_name_var)
        self.output_name_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self.translate_button = ttk.Button(self.root, text="Translate", command=self._on_translate,
                                            state="disabled")
        self.translate_button.pack(pady=(8, 4))

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill="x", padx=16, pady=(4, 4))

        self.status_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.status_var, foreground="#666").pack(padx=16, anchor="w")

        # Chunk list (left) + rendered preview (right)
        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=16, pady=(8, 16))

        chunks_container = ttk.Frame(paned)
        paned.add(chunks_container, weight=2)

        self.chunks_canvas = tk.Canvas(chunks_container, highlightthickness=0, bg="#ffffff")
        chunks_scrollbar = ttk.Scrollbar(chunks_container, orient="vertical", command=self.chunks_canvas.yview)
        self.chunks_inner = ttk.Frame(self.chunks_canvas)
        self.chunks_inner.bind(
            "<Configure>",
            lambda e: self.chunks_canvas.configure(scrollregion=self.chunks_canvas.bbox("all")),
        )
        self.chunks_window_id = self.chunks_canvas.create_window((0, 0), window=self.chunks_inner, anchor="nw")
        # Keep the inner frame's width tied to the canvas's actual visible width, so rows
        # use the real available space instead of clipping at whatever width they'd naturally
        # request (the canvas doesn't do this on its own for an embedded window).
        self.chunks_canvas.bind(
            "<Configure>",
            lambda e: self.chunks_canvas.itemconfig(self.chunks_window_id, width=e.width),
        )
        self.chunks_canvas.configure(yscrollcommand=chunks_scrollbar.set)
        self.chunks_canvas.pack(side="left", fill="both", expand=True)
        chunks_scrollbar.pack(side="right", fill="y")
        self._bind_scroll(self.chunks_canvas)

        self.chunks_placeholder = ttk.Label(
            self.chunks_inner, text="Chunks will appear here once you click Translate.",
            foreground="#999", wraplength=260, justify="left",
        )
        self.chunks_placeholder.pack(padx=8, pady=8, anchor="w")

        preview_container = ttk.Frame(paned)
        paned.add(preview_container, weight=2)

        from tkinterweb import HtmlFrame
        self.preview_frame = HtmlFrame(preview_container, messages_enabled=False)
        self.preview_frame.pack(fill="both", expand=True)
        self.preview_frame.load_html("<html><body></body></html>")

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

    # ---- claude status ----

    def _check_claude_async(self):
        def work():
            if not preflight.is_claude_installed():
                self.msg_queue.put(("claude_status", "not_installed", None))
                return
            status = preflight.claude_auth_status()
            if status.get("loggedIn"):
                self.msg_queue.put(("claude_status", "ok", status.get("email", "")))
            else:
                self.msg_queue.put(("claude_status", "not_logged_in", None))

        threading.Thread(target=work, daemon=True).start()

    def _start_login(self):
        self.login_button.pack_forget()
        self.claude_status_var.set("Opening Claude Code login (check your browser/terminal)...")

        def work():
            status = preflight.claude_login()
            if status.get("loggedIn"):
                self.msg_queue.put(("claude_status", "ok", status.get("email", "")))
            else:
                self.msg_queue.put(("claude_status", "not_logged_in", None))

        threading.Thread(target=work, daemon=True).start()

    # ---- chunk list ----

    def _clear_chunks(self):
        for chunk in self.chunks:
            if chunk.row_frame is not None:
                chunk.row_frame.destroy()
        self.chunks = []
        self.selected_chunk = None
        self.book = None
        self.chunks_placeholder.pack(padx=8, pady=8, anchor="w")
        self._render_preview_html("<html><body></body></html>")

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
        row = ttk.Frame(self.chunks_inner)
        row.pack(fill="x", padx=4, pady=2)

        info_label = ttk.Label(row, text=f"Chunk {chunk.index} (~{chunk.tokens:,} tok)",
                                cursor="hand2", anchor="w")
        info_label.pack(side="left", fill="x", expand=True)
        info_label.bind("<Button-1>", lambda e, c=chunk: self._select_chunk(c))

        status_label = ttk.Label(row, text="queued", foreground="#999999", width=8, anchor="w")
        status_label.pack(side="left")

        retranslate_button = ttk.Button(row, text="Retry", width=6,
                                         command=lambda c=chunk: self._on_retranslate_chunk(c))
        retranslate_button.pack(side="left", padx=(4, 0))

        chunk.row_frame = row
        chunk.info_label = info_label
        chunk.status_label = status_label
        chunk.retranslate_button = retranslate_button

    def _refresh_chunk_row(self, chunk: "ChunkInfo"):
        text, color = STATUS_LABELS.get(chunk.status, (chunk.status, "#666666"))
        if chunk.status_label is not None:
            chunk.status_label.config(text=text, foreground=color)
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
            and self._claude_ready
        )
        self.translate_button.config(state="normal" if ready else "disabled")

    def _on_translate(self):
        if self.full_translate_active or self.source_path is None:
            return
        target_lang = self.lang_var.get().strip()
        if not target_lang:
            return

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
            args=(self.source_path, target_lang, output_path),
            daemon=True,
        ).start()

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

    def _parse_and_translate_all(self, source_path, target_lang, output_path):
        try:
            import claude_driver
            import epub_io

            book = epub_io.load_book(str(source_path))
            chunks = claude_driver.pack_chunks(book.units, MAX_TOKENS_PER_CHUNK)
            chunk_tokens = [sum(claude_driver.estimate_tokens(u.html) for u in chunk) for chunk in chunks]
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
                translations, error = claude_driver.translate_chunk(chunk, target_lang, None)
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
        if not self._claude_ready:
            messagebox.showerror("Claude Code not ready", "Claude Code isn't installed or logged in yet.")
            return
        target_lang = self.lang_var.get().strip() or self.target_lang
        if not target_lang:
            messagebox.showerror("No language", "Pick a target language first.")
            return

        chunk.retranslating = True
        chunk.status = "translating"
        self._refresh_chunk_row(chunk)
        self._update_translate_state()

        def work():
            import claude_driver
            translations, error = claude_driver.translate_chunk(chunk.units, target_lang, None)
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
        elif kind == "claude_status":
            status, email = payload[0], payload[1]
            if status == "ok":
                self._claude_ready = True
                self.claude_status_var.set(f"Claude Code: logged in as {email}" if email else "Claude Code: logged in")
                self.login_button.pack_forget()
            elif status == "not_logged_in":
                self._claude_ready = False
                self.claude_status_var.set("Claude Code: not logged in")
                self.login_button.pack(after=self.claude_status_label, pady=(0, 8))
            elif status == "not_installed":
                self._claude_ready = False
                self.claude_status_var.set(preflight.CLAUDE_INSTALL_HINT)
                self.login_button.pack_forget()
            self._update_translate_state()


def main():
    _ensure_gui_deps()
    from tkinterdnd2 import TkinterDnD

    root = TkinterDnD.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
