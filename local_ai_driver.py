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
import urllib.error
import urllib.request

from translation_common import TranslationCancelled, TranslationError, translate_chunk_generic

CALL_TIMEOUT_SECONDS = 300


def _call_local_ai(system: str, user_prompt: str, model: str, base_url: str, api_key: str,
                    cancel_event=None) -> str:
    # Unlike the subprocess-based drivers, a blocking urlopen() can't be polled or
    # killed mid-flight from another thread -- this only catches a cancel that
    # happened before the request went out (e.g. while queued behind a retry
    # backoff). Once the HTTP call is actually in flight, Cancel won't interrupt it;
    # it'll still stop the chunk from retrying/splitting further once this call returns.
    if cancel_event is not None and cancel_event.is_set():
        raise TranslationCancelled("cancelled by user")

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
                     api_key: str = None, source_lang: str = None, cancel_event=None):
    """Same contract as claude_driver.translate_chunk / codex_driver.translate_chunk:
    returns (translations, error). Unlike those two, model and base_url are
    required here -- a local server has no sensible default for either.
    See translate_chunk_generic for the retry/auto-split/cancel behavior (and
    _call_local_ai's docstring for this engine's cancel caveat)."""
    if not base_url:
        return [u.html for u in units], TranslationError("no local AI base URL configured")
    if not model:
        return [u.html for u in units], TranslationError("no local AI model name configured")

    def call_fn(system, user_prompt):
        return _call_local_ai(system, user_prompt, model, base_url, api_key, cancel_event=cancel_event)

    return translate_chunk_generic(call_fn, units, target_lang, source_lang, cancel_event=cancel_event)
