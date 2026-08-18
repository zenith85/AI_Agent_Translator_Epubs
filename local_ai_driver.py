"""Translation via a local, OpenAI-compatible chat-completions server (Ollama,
LM Studio, vLLM, llama.cpp server, etc.).

This is the one engine here that actually needs an API key and an endpoint --
Claude/Codex ride an existing CLI login with no key at all, but a local server
has no notion of "your account," so the base URL, API key, and model name are
all passed in explicitly per call (the GUI/CLI collect them from the user)
rather than being auto-detected.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from translation_common import (
    MAX_RETRIES,
    TranslationError,
    strip_code_fence,
    system_prompt,
    wrong_script_detected,
)

CALL_TIMEOUT_SECONDS = 180


def _call_local_ai(system: str, user_prompt: str, model: str, base_url: str, api_key: str) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise TranslationError(f"local AI server returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TranslationError(f"could not reach local AI server at {base_url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise TranslationError(f"local AI server returned non-JSON response: {exc}") from exc

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TranslationError(f"unexpected response shape from local AI server: {str(body)[:300]}") from exc


def list_models(base_url: str, api_key: str, timeout: int = 15) -> list:
    """GET {base_url}/models -- used by the settings dialog's Test button to
    both verify connectivity/the API key and populate a model picker, so the
    user doesn't have to know/type an exact model name themselves."""
    if not base_url:
        raise TranslationError("no base URL given")
    url = base_url.rstrip("/") + "/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise TranslationError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TranslationError(f"could not reach {base_url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise TranslationError(f"server returned non-JSON response: {exc}") from exc

    try:
        return sorted(m["id"] for m in body["data"])
    except (KeyError, TypeError) as exc:
        raise TranslationError(f"unexpected response shape for model list: {str(body)[:300]}") from exc


def translate_chunk(units: list, target_lang: str, model: str = None, base_url: str = None,
                     api_key: str = None, source_lang: str = None):
    """Same contract as claude_driver.translate_chunk / codex_driver.translate_chunk:
    returns (translations, error). Unlike those two, model and base_url are
    required here -- a local server has no sensible default for either."""
    if not base_url:
        return [u.html for u in units], TranslationError("no local AI base URL configured")
    if not model:
        return [u.html for u in units], TranslationError("no local AI model name configured")

    system = system_prompt(target_lang, source_lang)
    user_prompt = json.dumps([u.html for u in units], ensure_ascii=False)

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            result_text = _call_local_ai(system, user_prompt, model, base_url, api_key)
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
        except Exception as exc:  # network/JSON/TranslationError, etc.
            last_error = exc

        if attempt < MAX_RETRIES:
            time.sleep(2.0 * (attempt + 1))

    return [u.html for u in units], last_error
