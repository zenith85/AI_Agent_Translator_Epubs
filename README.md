# INNO AI Agent Translator

Translates an EPUB with Claude or Codex while preserving the book's original HTML
formatting (bold/italic/links/footnotes/images), chapter structure, and metadata. It's not
a service with its own API key/billing — each translation chunk is handed to whichever
CLI you pick (`claude -p` or `codex exec`) as a one-shot, tool-less call, so it rides
whichever engine's existing login you already have instead of a separate API integration.

Two ways to run it: a drag-and-drop desktop window (`gui.py`), or a menu-driven command
line (`translate_epub.py`). Both let you pick the engine per run (and even per chunk, in
the GUI's re-translate button) and share the same parsing/translation code
(`epub_io.py`, `claude_driver.py`, `codex_driver.py`, `translation_common.py`, `preflight.py`).

## How it works

- **Reading**: chapter documents are read straight from the epub's zip (EbookLib is used
  only to resolve spine order and hrefs — its own content getter regenerates documents
  from a template and drops custom `<head>` content, so it's not used for the actual bytes).
- **Translation units**: each chapter is parsed and split into block-level elements
  (`p`, `li`, headings, `blockquote`, table cells, etc.), skipping anything inside
  `<script>/<style>/<pre>/<code>`. Each unit keeps its inline markup (`<em>`, `<a href>`, ...).
- **Chunking**: all units across the whole book are flattened into one ordered list and
  greedily packed into chunks up to a configurable token budget (`--max-tokens-per-chunk`,
  default 6000) — chunks aren't forced to align with chapter boundaries.
- **Engine choice**: pick Claude or Codex per run (and per chunk, via the GUI's
  re-translate button — handy for comparing how each engine handles a tricky passage).
  `claude_driver.py` calls `claude -p --allowedTools "" --system-prompt "..." --output-format
  json`; `codex_driver.py` calls `codex exec --sandbox read-only --skip-git-repo-check -o
  <tmpfile>` (Codex has no dedicated system-prompt flag, so the instructions are combined
  into one prompt sent over stdin, and the response is read back from the temp file to
  avoid Codex's human-oriented banner output on stdout). Both share the same chunk-packing,
  instructions text, and validation logic from `translation_common.py`.
- **Translation call**: each chunk's fragments are JSON-encoded and the engine is told to
  translate only the human-readable text, preserve every tag/attribute exactly, and reply
  with nothing but a JSON array of translated fragments in the same order. The reply is
  validated for length *and* checked that it's actually written in the target script (this
  caught a real bug: one chunk of a Korean-targeted book once came back entirely in
  Japanese) — either kind of mismatch retries up to twice, and if it still fails that
  chunk's original text is kept so one bad chunk never fails the whole job. Chunks run
  concurrently (`--concurrency`, default 3) since each call is an independent, stateless
  subprocess.
- **Writing**: the output epub is the original zip copied entry-by-entry, with only the
  translated chapter documents' (and `toc.ncx`'s navigation labels, which many readers use
  for their chapter list and which aren't part of the spine) bytes replaced — everything
  else (images, fonts, CSS, OPF, the required uncompressed `mimetype` entry) passes through
  byte-for-byte untouched.

## Setup

The only real prerequisite is Python 3.8+ and at least one of the two CLIs (both is
better, so you can switch). There's no API key to configure either way — each engine
uses whatever account you're already logged into.

```bash
cd ~/AI_Par_Ser_Trans
python3 gui.py            # the desktop app
# or: python3 translate_epub.py   # the command-line version
```

That's it — no manual venv/pip step required. On startup, both entry points check their
own prerequisites (for whichever engine you pick) and handle what they can:

- **CLI not installed** → prints install instructions (`npm install -g
  @anthropic-ai/claude-code` for Claude, or `npm install -g @openai/codex` for Codex; see
  https://claude.ai/code for Claude's current installer for your OS) and exits; the GUI
  shows the same message in its status area and keeps the Translate button disabled.
  Install it and re-run/refresh.
- **Not logged in** → the CLI automatically launches that engine's own login flow
  (`claude auth login` or `codex login`, opening your browser for the normal OAuth flow)
  and continues once it succeeds; the GUI shows a "Log in to Claude"/"Log in to Codex"
  button that does the same.
- **Missing Python packages** (`beautifulsoup4`, `lxml`, `EbookLib`, and for the GUI
  `tkinterdnd2`, `tkinterweb`) → installed via `pip install` automatically. If your system
  Python blocks that (e.g. an externally-managed environment), you'll be told to create a
  venv and run
  `pip install -r requirements.txt` yourself.

If you'd rather set up a venv up front instead of relying on the auto-install:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run — desktop app

```bash
python3 gui.py
```

Or double-click to launch it, no terminal needed:

- **Linux**: double-click `EPUB-Translator.desktop`. First time, your file manager may
  ask you to confirm it's trusted/executable (GNOME Files: right-click → "Allow
  Launching"; this is normal Linux behavior for any new launcher, not specific to this
  app). It runs `run_gui.sh`, which starts the GUI with no visible terminal and writes a
  `gui_launch.log` in the project folder if something goes wrong. You can also just
  double-click/run `run_gui.sh` directly — same thing, but it keeps a terminal window
  open so you see output live.
- **Windows**: double-click `run_gui.bat`. It finds `python` (or the `py` launcher) on
  PATH and runs `gui.py`; if Python isn't installed it tells you where to get it. The
  console window stays open if something fails, so errors are visible.

Both launchers just `cd` to the project folder and run the same `gui.py`, so they get all
the same auto-install/login-check behavior described above.

Pick an engine (Claude or Codex) from the dropdown — a status line shows whether it's
ready, with a login button if not. Drag an `.epub` onto the window (or click the drop zone
to browse) and it immediately shows chapter/page counts; pick a language (type your own if
it's not in the list); the "Save as" field is pre-filled with a suggested name but freely
editable. Click Translate: a progress bar and a live "chunk X/Y done · tokens · elapsed"
line track the run, and every chunk appears in a list on the left as it's queued,
translated, and finishes. Click any chunk to render its current translated HTML in the
preview pane on the right; click a chunk's **Retry** button to re-translate just that one
chunk with whichever engine is currently selected (switch engines first to compare how
each one handles a specific passage) — the output file is rewritten immediately after.

## Run — command line

Interactively — just run it and pick from the menus:

```bash
python translate_epub.py
```

```
+--------------------------------+
|    INNO AI Agent Translator    |
|  (powered by Claude or Codex)  |
+--------------------------------+

Step 1: which engine should translate?

  1) Claude
  2) Codex

> 1

Step 2: which file needs translation?

  1) book.epub
  2) Enter a path manually

> 1

Step 3: translate to which language?

   1) Spanish
   2) French
   3) German
   ...
  12) Other (type it in)

> 2

Translate 'book.epub' -> French using Claude? [Y/n]
```

Say no at the confirmation and it loops back to all three menus so you can pick again.
Any language not on the shortlist is one menu pick away via "Other".

Or non-interactively, for scripting:

```bash
python translate_epub.py book.epub --to "Brazilian Portuguese" --engine codex
```

Either way it writes `book.brazilian-portuguese.epub` next to the source by default.
Passing `--to`/`--engine`/the file path on the command line skips the matching question —
pass all three and it runs with no prompts at all. Other options:

- `--to <language>` — any free-text target language (omit to be asked).
- `--engine {claude,codex}` — which CLI to translate with (omit to be asked).
- `-o/--output <path>` — explicit output path.
- `--max-tokens-per-chunk <n>` (default 6000) — larger chunks mean fewer, slower calls with
  more content at risk if one fails; smaller chunks mean more calls but finer-grained
  progress and fallback.
- `--model <alias-or-name>` — passed through to the engine's CLI (e.g. `sonnet`, `opus`,
  `haiku` for Claude, or a Codex model name). Default: that account's default model.
- `--concurrency <n>` (default 3) — how many chunks translate in parallel.
- `--dry-run` — parse and chunk the book, print the plan (chapter/unit/chunk counts and
  estimated tokens), and exit without calling the engine or writing anything. Useful for
  sanity-checking a book before committing to a full run.

## Known limitations

- Chapters are re-serialized through BeautifulSoup's HTML parser, which normalizes markup
  somewhat (self-closing tags, quoting) and does not do strict XML validation — this is
  fine for essentially all epub readers but isn't guaranteed to produce byte-identical
  XHTML for translated chapters.
- If the model doesn't keep a fragment's original tag structure, the HTML parser used to
  re-insert the translation can end up splitting/reflowing that one unit unexpectedly. The
  system prompt explicitly asks Claude to preserve tags exactly, which handles this well in
  practice, but it isn't structurally enforced.
- Each chunk is a separate subprocess call; for Claude, the first call in a run pays a
  one-time prompt cache warm-up cost and later calls are cheaper as long as the system
  prompt stays identical (it does, by default, across a run). Codex has no equivalent
  dedicated system-prompt flag (the instructions are folded into the one prompt sent per
  call instead), so its per-call cost/latency profile hasn't been characterized the same
  way and may not benefit from caching the same way Claude's calls do.
- The wrong-script detector (catches e.g. a Korean-targeted chunk coming back in Japanese)
  only works for languages with a script distinctive enough to check for — CJK languages,
  Cyrillic, Arabic, Hindi, Greek, Thai, Hebrew. Two Latin-script languages getting mixed up
  (French coming back in Spanish, say) isn't something this can catch.
- Tested on Linux with Claude Code and Codex both installed via npm. Should work the same
  way on macOS and Windows (both CLIs ship native builds for both, and everything here is
  plain Python + subprocess calls to the `claude`/`codex` binaries), but that hasn't been
  verified.
- The auto-installed dependencies land wherever `sys.executable -m pip install` puts
  them — your system/user Python site-packages if you didn't make a venv first. Make a
  venv first (see above) if you'd rather keep it isolated.
- The GUI needs `tkinter` itself, which is part of the standard Python install on Windows
  and macOS but is sometimes a separate OS package on Linux (`sudo apt install python3-tk`
  on Debian/Ubuntu) — that one can't be auto-installed via pip, since it's not a PyPI
  package. Confirmed working on Linux with tkinter 8.6 + tkinterdnd2; drag-and-drop itself
  is unverified on macOS/Windows (clicking to browse for a file always works regardless).
- The GUI has no cancel/retry button mid-run and doesn't limit concurrency beyond a fixed
  default of 3 — for very large books, the CLI's `--concurrency`/`--max-tokens-per-chunk`
  flags give more control.
- `EPUB-Translator.desktop`'s `Exec`/`Path` are hardcoded to this install's absolute path
  (`/home/ibraheem/AI_Par_Ser_Trans`). If you move or copy the project folder, edit those
  two lines in the `.desktop` file to match the new location. `run_gui.sh` and
  `run_gui.bat` don't have this problem — they locate themselves at run time.
- `run_gui.sh` and `EPUB-Translator.desktop` were tested directly (launched the real
  window, and separately verified the no-tty error path doesn't hang). `run_gui.bat` was
  written by hand following standard Windows batch conventions but not run on an actual
  Windows machine — there's no Windows environment available here to test it on.
