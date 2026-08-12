"""Chunk packing (pure max-context packing) and translation via the Claude Code CLI.

Instead of calling the Anthropic API directly (which needs a separate API key/billing),
each chunk is handed to `claude -p` -- a one-shot, tool-less, headless call that rides
your existing Claude Code login. A tight custom --system-prompt tells it exactly what to
do and to respond with nothing but a JSON array, so output parsing stays simple and
reliable without needing the API's tool-use forcing.
"""

from __future__ import annotations

import json
import re
import subprocess
import time

CHARS_PER_TOKEN = 3.5  # rough heuristic for HTML-heavy source text
MAX_RETRIES = 2
CALL_TIMEOUT_SECONDS = 180

# Scripts distinctive enough that finding a real amount of the WRONG one in a
# translation is a strong signal the model translated into the wrong language
# for that chunk -- this is what actually caught a real bug: one chunk out of
# ~15 for a Korean-targeted book came back entirely in Japanese, and a
# length-only check on the returned array had no way to notice.
_SCRIPT_HINTS = [
    (("korean",), re.compile(r"[가-힣]")),                          # Hangul
    (("japanese",), re.compile(r"[぀-ヿ]")),                        # Hiragana/Katakana
    (("chinese", "mandarin", "cantonese"), re.compile(r"[一-鿿]")),  # CJK ideographs
    (("russian", "bulgarian", "ukrainian", "serbian"), re.compile(r"[Ѐ-ӿ]")),  # Cyrillic
    (("arabic",), re.compile(r"[؀-ۿ]")),                          # Arabic
    (("hindi",), re.compile(r"[ऀ-ॿ]")),                           # Devanagari
    (("greek",), re.compile(r"[Ͱ-Ͽ]")),                           # Greek
    (("thai",), re.compile(r"[฀-๿]")),                            # Thai
    (("hebrew",), re.compile(r"[֐-׿]")),                          # Hebrew
]

# A real amount of the wrong script, not just a stray quoted word/name.
_WRONG_SCRIPT_THRESHOLD = 20

# Hiragana/katakana unambiguously mean Japanese, even though Japanese text also
# uses kanji that overlaps with the Chinese-ideograph range below -- without this,
# a Japanese chunk mistranslated for a Chinese target would look "fine" (its kanji
# would satisfy the Chinese pattern) even though real Chinese text has no kana at all.
_KANA = re.compile(r"[぀-ヿ]")


def _expected_script(target_lang: str):
    lang_lower = target_lang.lower()
    for keywords, pattern in _SCRIPT_HINTS:
        if any(k in lang_lower for k in keywords):
            return pattern
    return None  # target language's script isn't one we can distinguish this way (e.g. Latin-script targets)


def _wrong_script_detected(translations: list, target_lang: str) -> bool:
    expected = _expected_script(target_lang)
    if expected is None:
        return False
    combined = "\n".join(translations)
    if expected.search(combined):
        is_chinese_target = any(k in target_lang.lower() for k in ("chinese", "mandarin", "cantonese"))
        if is_chinese_target and len(_KANA.findall(combined)) > _WRONG_SCRIPT_THRESHOLD:
            return True  # kanji satisfied the Chinese pattern, but this is actually Japanese
        return False  # expected script is present -- good enough
    for _, pattern in _SCRIPT_HINTS:
        if pattern is expected:
            continue
        if len(pattern.findall(combined)) > _WRONG_SCRIPT_THRESHOLD:
            return True
    return False


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def pack_chunks(units: list, max_context_tokens: int) -> list:
    """Greedily pack the global, reading-order unit list into chunks bounded
    by an estimated input-token budget. Chapter boundaries are not a split
    point -- a chunk may span the end of one chapter and the start of the next."""
    chunks = []
    current = []
    current_tokens = 0
    for unit in units:
        unit_tokens = estimate_tokens(unit.html)
        if current and current_tokens + unit_tokens > max_context_tokens:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        chunks.append(current)
    return chunks


def _system_prompt(target_lang: str) -> str:
    return (
        "You are a professional literary translation engine embedded in a script. "
        "You will receive a JSON array of HTML fragments, each one a single paragraph, "
        "heading, list item, or similar block from a book. "
        f"Translate the human-readable text of every fragment into {target_lang}, "
        "preserving all HTML tags and attributes exactly as-is. Never translate or alter "
        "tag names, attribute names, attribute values (href, src, ids, classes), or any "
        "code/markup -- only the natural-language text content. Keep the same tag "
        "structure and nesting. Maintain the tone, register, and meaning of the original "
        "as faithfully as possible.\n\n"
        "Respond with ONLY a raw JSON array of strings: no markdown code fences, no "
        "explanation, no extra keys. The array must have exactly one translated HTML "
        "string per input fragment, in the same order, and its length must exactly match "
        "the input array's length."
    )


class TranslationError(Exception):
    pass


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _call_claude(system_prompt: str, user_prompt: str, model: str = None) -> str:
    cmd = [
        "claude", "-p", user_prompt,
        "--allowedTools", "",
        "--system-prompt", system_prompt,
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


def translate_chunk(units: list, target_lang: str, model: str = None):
    """Run one chunk through `claude -p`. Returns (translations, error): on
    success error is None; on repeated failure, translations falls back to
    the original (untranslated) HTML for each unit and error explains why --
    the caller should log it but a bad chunk never has to fail the whole job."""
    system_prompt = _system_prompt(target_lang)
    user_prompt = json.dumps([u.html for u in units], ensure_ascii=False)

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            result_text = _call_claude(system_prompt, user_prompt, model)
            translations = json.loads(_strip_code_fence(result_text))
            if isinstance(translations, list) and len(translations) == len(units):
                if _wrong_script_detected(translations, target_lang):
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
