#!/usr/bin/env python3
"""Translate an EPUB chapter-by-chapter (packed to fit Claude's context) using the
Claude Code CLI (`claude -p`) -- no separate Anthropic API key needed, rides your
existing Claude Code login.

This script checks its own prerequisites (the `claude` CLI installed, you logged in,
and the local Python dependencies present) and fixes what it can before doing any
translation work, so it can be handed to a fresh machine without manual setup."""

from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import claude_driver  # stdlib-only, safe to import before dependency checks
import preflight  # stdlib-only, safe to import before dependency checks


def _ensure_claude_installed() -> None:
    if not preflight.is_claude_installed():
        print(preflight.CLAUDE_INSTALL_HINT + "\nThen re-run this script.", file=sys.stderr)
        sys.exit(1)


def _ensure_claude_logged_in() -> None:
    status = preflight.claude_auth_status()
    if status.get("loggedIn"):
        return

    print("Claude Code isn't logged in yet -- launching `claude auth login`...")
    status = preflight.claude_login()  # interactive: inherits this terminal, may open a browser

    if not status.get("loggedIn"):
        print(
            "error: still not logged in. Run `claude auth login` yourself, then re-run this script.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Logged in as {status.get('email', 'your account')}.")


def _ensure_dependencies() -> None:
    missing = preflight.missing_modules(preflight.EPUB_DEPS)
    if not missing:
        return

    print(f"Installing missing Python dependencies: {', '.join(missing)}...")
    if not preflight.install_modules(missing):
        print(
            "error: automatic install failed. Set up a virtualenv and run "
            "`pip install -r requirements.txt` yourself, then re-run this script.",
            file=sys.stderr,
        )
        sys.exit(1)


def derive_output_path(source: Path, target_lang: str, explicit: str) -> Path:
    if explicit:
        return Path(explicit)
    tag = re.sub(r"[^A-Za-z0-9]+", "-", target_lang.strip()).strip("-").lower() or "translated"
    return source.parent / f"{source.stem}.{tag}.epub"


BANNER = r"""
+----------------------------------------+
|            EPUB Translator             |
|          (powered by Claude)           |
+----------------------------------------+
"""

COMMON_LANGUAGES = [
    "Spanish",
    "French",
    "German",
    "Italian",
    "Portuguese (Brazil)",
    "Japanese",
    "Korean",
    "Simplified Chinese",
    "Arabic",
    "Russian",
    "Hindi",
]


def _menu_choice(prompt: str, option_count: int) -> int:
    """Read a 1..option_count menu choice, re-prompting until valid. Returns
    the 1-based index chosen."""
    while True:
        raw = input(prompt).strip()
        if raw.isdigit() and 1 <= int(raw) <= option_count:
            return int(raw)
        print(f"  Please enter a number from 1 to {option_count}.")


def _prompt_for_epub_path() -> Path:
    candidates = sorted(Path.cwd().glob("*.epub"))

    print("Step 1: which file needs translation?")
    print()
    for i, c in enumerate(candidates, start=1):
        print(f"  {i}) {c.name}")
    manual_option = len(candidates) + 1
    print(f"  {manual_option}) Enter a path manually")
    print()

    choice = _menu_choice("> ", manual_option)
    if choice != manual_option:
        return candidates[choice - 1]

    while True:
        raw = input("Path to the .epub file: ").strip().strip('"').strip("'")
        if not raw:
            print("  Please enter a path.")
            continue
        path = Path(raw).expanduser()
        if path.exists() and path.is_file():
            return path
        print(f"  '{raw}' doesn't exist, try again.")


def _prompt_for_target_lang() -> str:
    print()
    print("Step 2: translate to which language?")
    print()
    for i, lang in enumerate(COMMON_LANGUAGES, start=1):
        print(f"  {i:>2}) {lang}")
    other_option = len(COMMON_LANGUAGES) + 1
    print(f"  {other_option:>2}) Other (type it in)")
    print()

    choice = _menu_choice("> ", other_option)
    if choice != other_option:
        return COMMON_LANGUAGES[choice - 1]

    while True:
        raw = input("Language: ").strip()
        if raw:
            return raw
        print("  Please enter a language.")


def _confirm(source: Path, target_lang: str) -> bool:
    print()
    answer = input(f"Translate '{source.name}' -> {target_lang}? [Y/n] ").strip().lower()
    return answer in ("", "y", "yes")


def main():
    _ensure_claude_installed()
    _ensure_dependencies()
    _ensure_claude_logged_in()

    import epub_io  # deferred until _ensure_dependencies() has confirmed/installed bs4, lxml, ebooklib

    parser = argparse.ArgumentParser(description="Translate an EPUB using the Claude Code CLI.")
    parser.add_argument("epub", nargs="?", default=None,
                         help="Path to the source .epub file (omit to be asked)")
    parser.add_argument("--to", dest="target_lang", default=None,
                         help='Target language, e.g. "French", "Brazilian Portuguese" (omit to be asked)')
    parser.add_argument("-o", "--output", default="",
                         help="Output .epub path (default: <name>.<lang>.epub next to the source)")
    parser.add_argument("--max-tokens-per-chunk", type=int, default=6000,
                         help="Approx. input tokens packed per translation call (default: 6000)")
    parser.add_argument("--model", default=None,
                         help="Model alias/name passed to `claude -p --model` (default: your account's default model)")
    parser.add_argument("--concurrency", type=int, default=3,
                         help="Number of chunks to translate in parallel (default: 3)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Parse and chunk the book, print the plan, and exit without translating")
    args = parser.parse_args()

    interactive = not (args.epub and args.target_lang)

    try:
        if interactive:
            print(BANNER)
        while True:
            source = Path(args.epub) if args.epub else _prompt_for_epub_path()
            if not source.exists():
                print(f"error: {source} not found", file=sys.stderr)
                sys.exit(1)
            target_lang = args.target_lang or _prompt_for_target_lang()

            if not interactive or _confirm(source, target_lang):
                break
            print("\nLet's try again.\n")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {source.name}...")
    book = epub_io.load_book(str(source))
    chunks = claude_driver.pack_chunks(book.units, args.max_tokens_per_chunk)
    total_tokens = sum(claude_driver.estimate_tokens(u.html) for u in book.units)
    pages = epub_io.estimate_pages(book.units)
    print(
        f"{len(book.chapters)} chapter(s), ~{pages} page(s), {len(book.units)} translation unit(s), "
        f"~{total_tokens} estimated input tokens, packed into {len(chunks)} chunk(s)."
    )

    if args.dry_run:
        for i, chunk in enumerate(chunks, start=1):
            chunk_tokens = sum(claude_driver.estimate_tokens(u.html) for u in chunk)
            print(f"  chunk {i}: {len(chunk)} unit(s), ~{chunk_tokens} tokens")
        return

    output_path = derive_output_path(source, target_lang, args.output)

    if chunks:
        errors = []
        completed = 0

        def work(index, chunk):
            translations, error = claude_driver.translate_chunk(chunk, target_lang, args.model)
            return index, chunk, translations, error

        start = time.time()
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            futures = [pool.submit(work, i, chunk) for i, chunk in enumerate(chunks, start=1)]
            for future in as_completed(futures):
                index, chunk, translations, error = future.result()
                epub_io.apply_translations(chunk, translations)
                completed += 1
                if error is not None:
                    errors.append((index, error))
                    print(f"[{completed}/{len(chunks)}] chunk {index} FAILED, kept original text: {error}")
                else:
                    print(f"[{completed}/{len(chunks)}] chunk {index} translated")

        elapsed = time.time() - start
        ok = len(chunks) - len(errors)
        print(f"Translated {ok}/{len(chunks)} chunk(s) successfully in {elapsed:.1f}s.")
        if errors:
            print(f"{len(errors)} chunk(s) fell back to original text (see messages above).")
    else:
        print("No translatable text found; writing the book through unchanged.")

    print(f"Writing {output_path}...")
    epub_io.write_translated_epub(str(source), book.chapters, str(output_path))
    print("Done.")


if __name__ == "__main__":
    main()
