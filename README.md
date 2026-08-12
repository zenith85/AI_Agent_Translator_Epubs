# EPUB Translator

Translates an EPUB with Claude while preserving the book's original HTML formatting
(bold/italic/links/footnotes/images), chapter structure, and metadata. It's not a service
with its own API key/billing — each translation chunk is handed to the Claude Code CLI
(`claude -p`) as a one-shot, tool-less call, so it rides your existing Claude Code login
instead of a separate Anthropic API integration.

Two ways to run it: a drag-and-drop desktop window (`gui.py`), or a menu-driven command
line (`translate_epub.py`). Both share the same parsing/translation code
(`epub_io.py`, `claude_driver.py`, `preflight.py`).

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
- **Translation call**: each chunk's fragments are JSON-encoded and passed to
  `claude -p --allowedTools "" --system-prompt "..." --output-format json`. The system
  prompt tells Claude exactly what to do: translate only the human-readable text, preserve
  every tag/attribute exactly, and reply with nothing but a JSON array of translated
  fragments in the same order. The reply is parsed and validated for length; a mismatch or
  CLI error retries up to twice, and if it still fails that chunk's original text is kept
  so one bad chunk never fails the whole job. Chunks run concurrently (`--concurrency`,
  default 3) since each call is an independent, stateless subprocess.
- **Writing**: the output epub is the original zip copied entry-by-entry, with only the
  translated chapter documents' bytes replaced — everything else (images, fonts, CSS, nav,
  OPF, the required uncompressed `mimetype` entry) passes through byte-for-byte untouched.

## Setup

The only real prerequisite is Python 3.8+ and the Claude Code CLI. There's no API key to
configure — `claude -p` uses whatever account you're already logged into.

```bash
cd ~/AI_Par_Ser_Trans
python3 gui.py            # the desktop app
# or: python3 translate_epub.py   # the command-line version
```

That's it — no manual venv/pip step required. On startup, both entry points check their
own prerequisites and handle what they can:

- **`claude` not installed** → the CLI prints install instructions (`npm install -g
  @anthropic-ai/claude-code`, or see https://claude.ai/code) and exits; the GUI shows the
  same message in its status area and keeps the Translate button disabled. Install it and
  re-run/refresh.
- **Not logged in** → the CLI automatically launches `claude auth login` (opens your
  browser for the normal OAuth flow) and continues once it succeeds; the GUI shows a
  "Log in to Claude Code" button that does the same.
- **Missing Python packages** (`beautifulsoup4`, `lxml`, `EbookLib`, and for the GUI
  `tkinterdnd2`) → installed via `pip install` automatically. If your system Python blocks
  that (e.g. an externally-managed environment), you'll be told to create a venv and run
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

Drag an `.epub` onto the window (or click the drop zone to browse), pick a language from
the dropdown (type your own if it's not in the list), and click Translate. A status line
at the top shows whether Claude Code is ready; progress and a running log appear under the
Translate button while it works, and a dialog shows the output path when it's done.

## Run — command line

Interactively — just run it and pick from the menus:

```bash
python translate_epub.py
```

```
+----------------------------------------+
|            EPUB Translator             |
|          (powered by Claude)           |
+----------------------------------------+

Step 1: which file needs translation?

  1) book.epub
  2) Enter a path manually

> 1

Step 2: translate to which language?

   1) Spanish
   2) French
   3) German
   ...
  12) Other (type it in)

> 2

Translate 'book.epub' -> French? [Y/n]
```

Say no at the confirmation and it loops back to the two menus so you can pick again.
Any language not on the shortlist is one menu pick away via "Other".

Or non-interactively, for scripting:

```bash
python translate_epub.py book.epub --to "Brazilian Portuguese"
```

Either way it writes `book.brazilian-portuguese.epub` next to the source by default.
Passing `--to` and/or the file path on the command line skips the matching question.
Other options:

- `--to <language>` (required) — any free-text target language.
- `-o/--output <path>` — explicit output path.
- `--max-tokens-per-chunk <n>` (default 6000) — larger chunks mean fewer, slower calls with
  more content at risk if one fails; smaller chunks mean more calls but finer-grained
  progress and fallback.
- `--model <alias-or-name>` — passed through to `claude -p --model` (e.g. `sonnet`,
  `opus`, `haiku`, or a full model name). Default: your account's default model.
- `--concurrency <n>` (default 3) — how many chunks translate in parallel.
- `--dry-run` — parse and chunk the book, print the plan (chapter/unit/chunk counts and
  estimated tokens), and exit without calling Claude or writing anything. Useful for
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
- Each chunk is a separate `claude -p` subprocess; the first call in a run pays a one-time
  prompt cache warm-up cost, and later calls in the same run are cheaper as long as the
  system prompt stays identical (it does, by default, across a run).
- Tested on Linux with Claude Code installed via npm. Should work the same way on macOS
  and Windows (Claude Code ships native builds for both, and everything here is plain
  Python + subprocess calls to the `claude` binary), but that hasn't been verified.
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
