"""Translation via the OpenAI Codex CLI (`codex exec`) -- an alternative engine
to claude_driver.py, so translation isn't locked to one provider.

Like claude_driver.py, this rides your existing login (ChatGPT/Codex
subscription) rather than a separate OpenAI API key, and never touches disk
beyond a throwaway temp file used to read the response back.

Codex's CLI has no dedicated --system-prompt flag (unlike `claude -p`), so the
instructions and the content to translate are combined into one prompt sent
over stdin. `--sandbox read-only` and `--skip-git-repo-check` keep each call a
plain text-in/text-out operation with no side effects. `-o <file>` is used to
get exactly the model's final response with none of Codex's human-oriented
banner/log output that shows up on stdout.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

from translation_common import (
    TranslationCancelled,
    TranslationError,
    estimate_tokens,  # noqa: F401 -- re-exported for callers that only import codex_driver
    pack_chunks,  # noqa: F401
    translate_chunk_generic,
)

CALL_TIMEOUT_SECONDS = 300
_POLL_INTERVAL = 0.2

# See claude_driver._POPEN_KWARGS -- same reasoning: `codex` resolves to a
# codex.cmd shim on Windows, which Windows would otherwise pop a new visible
# console window for.
_POPEN_KWARGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _call_codex(full_prompt: str, model: str = None, cancel_event=None) -> str:
    fd, tmp_path = tempfile.mkstemp(prefix="codex_out_", suffix=".txt")
    os.close(fd)
    try:
        cmd = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-o", tmp_path]
        if model:
            cmd += ["-m", model]

        # See claude_driver._call_claude for why this is a poll loop instead of a
        # single blocking subprocess.run(..., timeout=...): only a loop gives Cancel
        # a chance to actually kill the process instead of just waiting it out.
        #
        # encoding/errors are explicit for the same reason as claude_driver: `text=True`
        # alone would encode/decode stdin/stdout/stderr with the OS's locale-preferred
        # encoding (e.g. cp949 on Korean Windows) instead of UTF-8, which can silently
        # crash subprocess's internal reader thread on non-ASCII content and leave the
        # call hung.
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", **_POPEN_KWARGS,
        )
        start = time.time()
        pending_input = full_prompt  # communicate() only accepts input on its first call
        stderr = ""
        while True:
            try:
                _, stderr = proc.communicate(input=pending_input, timeout=_POLL_INTERVAL)
                break
            except subprocess.TimeoutExpired:
                pending_input = None
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    proc.communicate()
                    raise TranslationCancelled("cancelled by user")
                if time.time() - start > CALL_TIMEOUT_SECONDS:
                    proc.kill()
                    proc.communicate()
                    raise subprocess.TimeoutExpired(cmd, CALL_TIMEOUT_SECONDS)

        if proc.returncode != 0:
            raise TranslationError(f"codex exited {proc.returncode}: {stderr.strip()[:500]}")

        with open(tmp_path, "r", encoding="utf-8") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def translate_chunk(units: list, target_lang: str, model: str = None, source_lang: str = None,
                     cancel_event=None):
    """Run one chunk through `codex exec`. Same contract as
    claude_driver.translate_chunk: returns (translations, error) -- on
    success error is None; on repeated failure, translations falls back to
    the original (untranslated) HTML for each unit and error explains why.
    See translate_chunk_generic for the retry/auto-split/cancel behavior."""
    def call_fn(system, user_prompt):
        return _call_codex(system + "\n\nInput:\n" + user_prompt, model, cancel_event=cancel_event)

    return translate_chunk_generic(call_fn, units, target_lang, source_lang, cancel_event=cancel_event)
