"""Engine-agnostic pieces shared between claude_driver.py and codex_driver.py:
chunk packing, token estimation, the translation instructions, and the
script-based wrong-language detector. Each driver keeps its own CLI transport
(and its own translate_chunk retry loop) since the two CLIs differ enough in
how they take a system prompt that unifying the call signature would cost
Claude Code its prompt-caching benefit -- but everything else is identical."""

from __future__ import annotations

import json
import re
import time

CHARS_PER_TOKEN = 3.5  # rough heuristic for HTML-heavy source text
MAX_RETRIES = 2


class TranslationError(Exception):
    pass


class TranslationCancelled(TranslationError):
    """Raised/returned when a caller-supplied cancel_event was set mid-translation
    -- distinct from a real failure so the GUI can show "cancelled" instead of
    "failed" and skip the error popup."""


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


def translate_chunk_generic(call_fn, units: list, target_lang: str, source_lang: str = None,
                             max_retries: int = MAX_RETRIES, cancel_event=None):
    """Shared retry loop used by every driver's translate_chunk. call_fn(system,
    user_prompt) -> raw response text, and may raise on any transport failure
    (subprocess timeout, non-zero exit, HTTP error, bad JSON, etc). call_fn is
    expected to check cancel_event itself so an in-flight subprocess/request can
    actually be aborted, not just skipped between attempts.

    If a chunk still fails after max_retries and holds more than one unit, it's
    bisected and each half is retried independently (same retry budget each) --
    this is what actually recovers a chunk that's too big for the model to
    finish translating inside the call timeout, instead of just re-sending the
    same oversized chunk until the retry budget runs out. Only a single unit
    that fails on its own falls back to its original (untranslated) HTML.

    cancel_event (a threading.Event) is checked before every attempt and during
    the backoff sleep between attempts -- once set, retries/splits stop
    immediately and the original text is kept for whatever wasn't translated yet.
    """
    if cancel_event is not None and cancel_event.is_set():
        return [u.html for u in units], TranslationCancelled("cancelled by user")

    system = system_prompt(target_lang, source_lang)
    user_prompt = json.dumps([u.html for u in units], ensure_ascii=False)

    last_error = None
    for attempt in range(max_retries + 1):
        if cancel_event is not None and cancel_event.is_set():
            last_error = TranslationCancelled("cancelled by user")
            break
        try:
            result_text = call_fn(system, user_prompt)
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
        except Exception as exc:  # subprocess/JSON/HTTP errors, TranslationError, etc.
            last_error = exc
            if isinstance(exc, TranslationCancelled):
                break

        if attempt < max_retries:
            # cancel_event.wait() sleeps for the backoff *unless* cancelled first, in
            # which case it wakes immediately instead of idling out the full delay.
            if cancel_event is not None:
                if cancel_event.wait(2.0 * (attempt + 1)):
                    last_error = TranslationCancelled("cancelled by user")
                    break
            else:
                time.sleep(2.0 * (attempt + 1))

    if isinstance(last_error, TranslationCancelled):
        return [u.html for u in units], last_error

    if len(units) > 1:
        mid = len(units) // 2
        left, left_error = translate_chunk_generic(
            call_fn, units[:mid], target_lang, source_lang, max_retries, cancel_event)
        right, right_error = translate_chunk_generic(
            call_fn, units[mid:], target_lang, source_lang, max_retries, cancel_event)
        return left + right, left_error or right_error

    return [u.html for u in units], last_error


AUTO_DETECT = "Auto-detect"


def is_auto_detect(source_lang: str) -> bool:
    return not source_lang or source_lang.strip().lower() in ("", "auto-detect", "auto detect", "auto")


def system_prompt(target_lang: str, source_lang: str = None) -> str:
    if is_auto_detect(source_lang):
        translate_clause = f"Translate the human-readable text of every fragment into {target_lang}, "
    else:
        translate_clause = (
            f"Translate the human-readable text of every fragment from {source_lang} "
            f"into {target_lang}, "
        )
    return (
        "You are a professional literary translation engine embedded in a script. "
        "You will receive a JSON array of HTML fragments, each one a single paragraph, "
        "heading, list item, or similar block from a book. "
        + translate_clause +
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


def strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


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


def wrong_script_detected(translations: list, target_lang: str) -> bool:
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
