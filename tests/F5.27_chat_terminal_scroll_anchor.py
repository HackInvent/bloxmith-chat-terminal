#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Role: Verifies chat terminal modal scroll anchoring contract.
# File Name: F5.27_chat_terminal_scroll_anchor.py
# Author: Alexandre EL
# Email: alex@hackinvent.com
# Created Date: 2026-06-23
# -----------------------------------------------------------------------------

"""F5.27 - Chat terminal history refresh preserves the reader position."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    source = (ROOT / "assets/js/block_modal.js").read_text()
    expect("function captureScrollAnchor" in source, "History refresh must capture a visible message anchor.")
    expect("function restoreScrollAnchor" in source, "History refresh must restore a captured message anchor.")
    expect('querySelectorAll("[data-chat-terminal-message-id]")' in source, "Anchor must use stable message ids, not raw scrollTop only.")
    expect("stream.dataset.chatTerminalHistoryHtml === html" in source, "Unchanged history HTML must still preserve bottom-stick behavior.")
    expect("function appendMessage(stream, entry, { stickToBottom = true } = {})" in source, "Local append must accept explicit scroll control.")
    expect("appendMessage(stream, entry, { stickToBottom: shouldStickToBottom })" in source, "Refresh must not force-scroll local optimistic messages while reading older history.")
    expect("scrollBottomThreshold" in source, "Near-bottom threshold must stay centralized.")
    print("[ok] F5.27_chat_terminal_scroll_anchor")


if __name__ == "__main__":
    main()
