"""Translation via the Claude Code CLI (`claude -p`).

Instead of calling the Anthropic API directly (which needs a separate API key/billing),
each chunk is handed to `claude -p` -- a one-shot, tool-less, headless call that rides
your existing Claude Code login. A tight custom --system-prompt tells it exactly what to
do and to respond with nothing but a JSON array, so output parsing stays simple and
reliable without needing the API's tool-use forcing.

Chunk packing, the instructions text, and the wrong-language check are shared with
codex_driver.py via translation_common.py; this module is just the Claude-specific
subprocess transport.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

from translation_common import (
    TranslationCancelled,
    TranslationError,
    estimate_tokens,  # noqa: F401 -- re-exported for callers that only import claude_driver
    pack_chunks,  # noqa: F401
    translate_chunk_generic,
)

CALL_TIMEOUT_SECONDS = 300

# On Windows, `claude` resolves to a `claude.cmd` shim; launching it from a
# console-less GUI process makes Windows auto-open a new visible console
# window for it. CREATE_NO_WINDOW suppresses that. We also explicitly close
# stdin (the call never needs input) so that if the CLI ever tries to prompt
# for something interactive (e.g. a first-run workspace-trust confirmation),
# it fails fast instead of hanging forever reading from a console nobody's
# watching -- which otherwise looks exactly like a stuck translation that's
# silently burning usage on repeated timeout-and-retry cycles.
_POPEN_KWARGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# How often the poll loop below wakes up to check for cancellation/timeout while
# `claude -p` is still running. Small enough that Cancel feels immediate, large
# enough not to busy-loop.
_POLL_INTERVAL = 0.2


def _call_claude(system: str, user_prompt: str, model: str = None, cancel_event=None) -> str:
    cmd = [
        "claude", "-p", user_prompt,
        "--allowedTools", "",
        "--system-prompt", system,
        "--output-format", "json",
    ]
    if model:
        cmd += ["--model", model]

    # subprocess.run(..., timeout=...) blocks until the process exits with no way to
    # interrupt it early, so Cancel couldn't actually stop a running call. Popen +
    # a polling communicate() loop (an officially supported retry pattern -- see the
    # subprocess docs on TimeoutExpired) lets us check cancel_event/the timeout every
    # _POLL_INTERVAL and kill the process the moment either fires.
    # encoding/errors are explicit because `text=True` alone decodes with the OS's
    # locale-preferred encoding (e.g. cp949 on Korean Windows) -- claude's actual
    # output is UTF-8, and any translated character outside that locale's codepage
    # would otherwise crash the decode inside subprocess's internal reader thread.
    # That crash is swallowed silently (just a background-thread traceback), leaving
    # stdout/stderr as None or the pipe undrained -- which can make the still-running
    # claude process block forever on a full pipe, looking exactly like a hung
    # translation that nonetheless already burned real usage server-side.
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **_POPEN_KWARGS,
    )
    start = time.time()
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=_POLL_INTERVAL)
            break
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                proc.communicate()
                raise TranslationCancelled("cancelled by user")
            if time.time() - start > CALL_TIMEOUT_SECONDS:
                proc.kill()
                proc.communicate()
                raise subprocess.TimeoutExpired(cmd, CALL_TIMEOUT_SECONDS)

    if proc.returncode != 0:
        raise TranslationError(f"claude exited {proc.returncode}: {stderr.strip()[:500]}")

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TranslationError(f"could not parse claude's JSON envelope: {exc}") from exc

    if envelope.get("is_error"):
        raise TranslationError(f"claude reported an error: {str(envelope.get('result'))[:500]}")

    return envelope.get("result", "")


def translate_chunk(units: list, target_lang: str, model: str = None, source_lang: str = None,
                     cancel_event=None):
    """Run one chunk through `claude -p`. Returns (translations, error): on
    success error is None; on repeated failure, translations falls back to
    the original (untranslated) HTML for each unit and error explains why --
    the caller should log it but a bad chunk never has to fail the whole job.
    See translate_chunk_generic for the retry/auto-split/cancel behavior."""
    def call_fn(system, user_prompt):
        return _call_claude(system, user_prompt, model, cancel_event=cancel_event)

    return translate_chunk_generic(call_fn, units, target_lang, source_lang, cancel_event=cancel_event)
