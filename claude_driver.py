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
import time

from translation_common import (
    MAX_RETRIES,
    TranslationError,
    estimate_tokens,  # noqa: F401 -- re-exported for callers that only import claude_driver
    pack_chunks,  # noqa: F401
    strip_code_fence,
    system_prompt,
    wrong_script_detected,
)

CALL_TIMEOUT_SECONDS = 180


def _call_claude(system: str, user_prompt: str, model: str = None) -> str:
    cmd = [
        "claude", "-p", user_prompt,
        "--allowedTools", "",
        "--system-prompt", system,
        "--output-format", "json",
    ]
    if model:
        cmd += ["--model", model]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise TranslationError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TranslationError(f"could not parse claude's JSON envelope: {exc}") from exc

    if envelope.get("is_error"):
        raise TranslationError(f"claude reported an error: {str(envelope.get('result'))[:500]}")

    return envelope.get("result", "")


def translate_chunk(units: list, target_lang: str, model: str = None, source_lang: str = None):
    """Run one chunk through `claude -p`. Returns (translations, error): on
    success error is None; on repeated failure, translations falls back to
    the original (untranslated) HTML for each unit and error explains why --
    the caller should log it but a bad chunk never has to fail the whole job."""
    system = system_prompt(target_lang, source_lang)
    user_prompt = json.dumps([u.html for u in units], ensure_ascii=False)

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            result_text = _call_claude(system, user_prompt, model)
            translations = json.loads(strip_code_fence(result_text))
            if isinstance(translations, list) and len(translations) == len(units):
                if wrong_script_detected(translations, target_lang):
                    last_error = TranslationError(
                        f"response doesn't look like it's actually in {target_lang} "
                        "(wrong script detected) -- retrying"
                    )
                else:
                    return translations, None
            else:
                got = len(translations) if isinstance(translations, list) else type(translations).__name__
                last_error = TranslationError(f"expected {len(units)} translations, got {got}")
        except subprocess.TimeoutExpired as exc:
            last_error = exc
        except Exception as exc:  # subprocess/JSON errors, TranslationError, etc.
            last_error = exc

        if attempt < MAX_RETRIES:
            time.sleep(2.0 * (attempt + 1))

    return [u.html for u in units], last_error
