"""Shared prerequisite checks: is Claude Code installed/logged in, are the
required Python packages present. Used by both the CLI (translate_epub.py)
and the GUI (gui.py), which each present the results differently (console
text vs. dialogs/labels) -- this module only checks and fixes, it doesn't print."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys

CLAUDE_INSTALL_URL = "https://claude.ai/code"
CLAUDE_INSTALL_HINT = (
    "Claude Code isn't installed (the `claude` command isn't on your PATH).\n"
    "Commonly installed with:\n"
    "    npm install -g @anthropic-ai/claude-code\n"
    f"See {CLAUDE_INSTALL_URL} for the current installer for your OS."
)

CODEX_INSTALL_HINT = (
    "Codex isn't installed (the `codex` command isn't on your PATH).\n"
    "Commonly installed with:\n"
    "    npm install -g @openai/codex"
)

EPUB_DEPS = [("bs4", "beautifulsoup4"), ("lxml", "lxml"), ("ebooklib", "EbookLib")]


def is_claude_installed() -> bool:
    return shutil.which("claude") is not None


def claude_auth_status() -> dict:
    try:
        proc = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=30)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return {}


def claude_login() -> dict:
    """Runs `claude auth login` inheriting stdio (so any link/code it prints is
    visible in the terminal that launched us) and returns the refreshed status."""
    subprocess.run(["claude", "auth", "login"])
    return claude_auth_status()


def claude_usage_status() -> dict:
    """There's no JSON API for plan usage -- `/usage` is a slash command whose
    reply is prose, so this runs it through `claude -p` (same one-shot,
    tool-less transport claude_driver.py uses for translation) and regexes
    the percentages/reset times back out of the text."""
    try:
        proc = subprocess.run(
            ["claude", "-p", "/usage", "--allowedTools", "", "--output-format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        envelope = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return {}
    if envelope.get("is_error"):
        return {}

    text = envelope.get("result", "")
    session_match = re.search(r"Current session:\s*(\d+)%\s*used(?:\s*\S\s*resets\s*(.+))?", text)
    week_match = re.search(r"Current week[^:\n]*:\s*(\d+)%\s*used(?:\s*\S\s*resets\s*(.+))?", text)
    return {
        "session_pct": int(session_match.group(1)) if session_match else None,
        "session_reset": (session_match.group(2) or "").strip() if session_match else "",
        "week_pct": int(week_match.group(1)) if week_match else None,
        "week_reset": (week_match.group(2) or "").strip() if week_match else "",
        "raw": text,
    }


def is_codex_installed() -> bool:
    return shutil.which("codex") is not None


def codex_login_status() -> dict:
    """Codex's CLI has no --json for login status, so this parses its plain-text
    output (e.g. "Logged in using ChatGPT"). Normalized to the same {"loggedIn",
    "email"} shape as claude_auth_status() so callers can treat both uniformly --
    Codex just never has an email, only a "detail" describing the auth method."""
    try:
        proc = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    # Codex prints its status line to stderr, not stdout -- check both.
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if "logged in" in output.lower():
        detail = output.split(" using ", 1)[-1].strip() if " using " in output.lower() else output
        return {"loggedIn": True, "email": "", "detail": detail}
    return {"loggedIn": False, "email": "", "detail": output}


def codex_login() -> dict:
    """Runs `codex login` inheriting stdio and returns the refreshed status."""
    subprocess.run(["codex", "login"])
    return codex_login_status()


# Lets callers (gui.py, translate_epub.py) treat either engine uniformly instead
# of hardcoding if/elif branches per engine -- add a new engine here later by
# adding one more entry, not by editing every call site.
ENGINES = {
    "claude": {
        "label": "Claude",
        "cli_hint": CLAUDE_INSTALL_HINT,
        "is_installed": is_claude_installed,
        "auth_status": claude_auth_status,
        "login": claude_login,
    },
    "codex": {
        "label": "Codex",
        "cli_hint": CODEX_INSTALL_HINT,
        "is_installed": is_codex_installed,
        "auth_status": codex_login_status,
        "login": codex_login,
    },
}


def missing_modules(modules: list) -> list:
    """modules: list of (import_name, pip_name) pairs. Returns the pip_names
    of the ones that fail to import."""
    missing = []
    for module_name, pip_name in modules:
        try:
            __import__(module_name)
        except ImportError:
            missing.append(pip_name)
    return missing


def install_modules(pip_names: list) -> bool:
    """Best-effort `pip install`. Returns True on success (or if nothing to do)."""
    if not pip_names:
        return True
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", *pip_names], check=True)
        return True
    except subprocess.CalledProcessError:
        return False
