"""Shared prerequisite checks: is Claude Code installed/logged in, are the
required Python packages present. Used by both the CLI (translate_epub.py)
and the GUI (gui.py), which each present the results differently (console
text vs. dialogs/labels) -- this module only checks and fixes, it doesn't print."""

from __future__ import annotations

import json
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
