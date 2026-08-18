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

import json
import os
import subprocess
import tempfile
import time

from translation_common import (
    MAX_RETRIES,
    TranslationError,
    estimate_tokens,  # noqa: F401 -- re-exported for callers that only import codex_driver
    pack_chunks,  # noqa: F401
    strip_code_fence,
    system_prompt,
    wrong_script_detected,
)

CALL_TIMEOUT_SECONDS = 180


def _call_codex(full_prompt: str, model: str = None) -> str:
    fd, tmp_path = tempfile.mkstemp(prefix="codex_out_", suffix=".txt")
    os.close(fd)
    try:
        cmd = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-o", tmp_path]
        if model:
            cmd += ["-m", model]

        proc = subprocess.run(
            cmd, input=full_prompt, capture_output=True, text=True, timeout=CALL_TIMEOUT_SECONDS
        )
        if proc.returncode != 0:
            raise TranslationError(f"codex exited {proc.returncode}: {proc.stderr.strip()[:500]}")

        with open(tmp_path, "r", encoding="utf-8") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def translate_chunk(units: list, target_lang: str, model: str = None, source_lang: str = None):
    """Run one chunk through `codex exec`. Same contract as
    claude_driver.translate_chunk: returns (translations, error) -- on
    success error is None; on repeated failure, translations falls back to
    the original (untranslated) HTML for each unit and error explains why."""
    prompt = (system_prompt(target_lang, source_lang) + "\n\nInput:\n"
              + json.dumps([u.html for u in units], ensure_ascii=False))

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            result_text = _call_codex(prompt, model)
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
