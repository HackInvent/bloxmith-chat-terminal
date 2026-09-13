# Chat Terminal

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->


An autonomous conversation block with fixed `msg` input/output ports. It publishes structured user messages and displays incoming messages.

## Ports

- Input **`msg`**, ID 1: `message/*`, `application/json`, `text/plain`; multiplicity `many`.
- Output **`msg`**, ID 1: `application/json`, `message/*`; multiplicity `many`.
- Ports are fixed; the block creates no dynamic ports.

## Message format

User messages are compact JSON:

```json
{"origin_id":"chat-1","origin_name":"Chat terminal","msg":"hello"}
```

Attachments add an optional descriptor list, never inline file binaries:

```json
{
  "origin_id": "chat-1",
  "origin_name": "Chat terminal",
  "msg": "analyze this image",
  "attachments": [
    {
      "id": "att_...",
      "name": "capture.png",
      "mime": "image/png",
      "size": 12345,
      "uri": "attachments/att_.../capture.png",
      "url": "/api/projects/.../graphs/.../instances/1/attachments/att_.../capture.png",
      "scope": {
        "workspace_project_id": "...",
        "graph_id": "...",
        "instance_id": "1"
      }
    }
  ]
}
```

Incoming messages in this format display `origin_id`, `origin_name`, `msg` and attachments. Plain text, invalid JSON, or JSON containing neither `msg` nor `attachments` is displayed as raw content from `unkbloc`.

Every displayed message receives a local message ID stored in history. That ID is not added to the published schema, preserving compatibility with existing consumers.

## Card and modal

The canvas card shows the node title, last-message preview and visible history count. `node_card.html` uses the autonomous helper placeholders `__title__`, `__preview__` and `__length__`.

`block_modal.html` and `assets/js/block_modal.js` provide:

- A default **Terminal** tab for conversation and complete message text.
- A separate **Attributes** tab (`Attributs` in the current UI) for identity, configuration, ports and state.
- A **Logs** tab for the last runtime error, replacing the generic Error tab.
- Readable message cards with technical IDs in collapsed details.
- Safe Markdown: headings, lists, quotes, code, simple tables, emphasis and `http`/`https`/`mailto` links. The original `msg` remains full plain text; user HTML is escaped.
- A compact, auto-growing composer with local scrolling.
- A Send button (`Envoyer`), attachment picker, image/file paste and drag-and-drop uploads.
- Enter to send; Shift+Enter for a newline.
- Chronological history, newest at the bottom, with a bounded scrollable viewport.
- Bottom anchoring by default. When reading older content, the visible message remains the scroll anchor across refreshes.
- The latest 20 messages only.
- Autonomous refresh through `/api/blocks/chat_terminal/history`, without rebuilding the modal during global runtime polling.
- One history poller per modal, paused in hidden browser tabs and stopped when the modal or page disappears.

Sending updates `config.pending_message`, `config.pending_message_id` and recent node UI history. In `centralized`, or when publishing a seed in `zeromq_active`, that pending message is emitted on `msg`.

When Active Runtime is already loaded, modal JavaScript avoids a graph patch and uses generic `publish_output` directly. The block owns the business payload and retains optimistic messages locally so autonomous refresh does not erase them. Server action `chat_send_message` also permits its `ui_history/pending_message` patch during an active run when direct active publication is not used.

## Speech-to-text

Hold the **Mic** button for a short browser dictation. MediaRecorder captures temporary audio, uploads the blob through `chat_transcribe_audio`, and `block.py` calls the OpenAI-compatible `/v1/audio/transcriptions` endpoint.

The transcript fills the composer; it is **not automatically sent** on `msg`. The user can edit it before sending.

STT settings are in Attributes → Configuration:

- `speech_model`: `gpt-4o-transcribe`, `gpt-4o-mini-transcribe` or `whisper-1`.
- `speech_api_key`: server-side API key; falls back to `OPENAI_API_KEY` when empty.
- `speech_api_base_url`: OpenAI-compatible base URL; default `https://api.openai.com`.
- `speech_language`: optional language, for example `en`.
- `speech_prompt`: optional transcription context.
- `speech_timeout_sec`: HTTP timeout, bounded to 1–3,600 seconds.

The key is not rendered in modal HTML or returned by actions. This short terminal-local capture does not reuse the audio splitting, FFmpeg or advanced features of OpenAI STT.

## Persistence

A block-owned JSON Lines history stores sent/received messages and attachment descriptors, deduplicated by `message_id`. The framework attachments service stores file contents in the graph instance directory.

The default history path is:

```text
user/projects/chat_terminal_history/{node_id}.jsonl
```

It is relative to, and confined within, runtime `root_dir`. Override it with `config.history_path`; `{node_id}` and `{run_id}` are expanded at execution. No functional file-size limit is advertised.

The UI loads only the latest 20 messages from runtime metadata or recent node history. Its block-owned history endpoint rereads the file without central modal reconstruction. Polling is idempotent and scoped to the visible modal instance to avoid concurrent refreshes during long active sessions. Direct active sending also calls block-owned endpoint `remember` to persist the message before later refreshes.

## Runtime behavior

Both `centralized` and `zeromq_active` use the generic runtime. Incoming messages are parsed, displayed and persisted, but **not automatically republished**. Only pending user input is sent on `msg`.

Active Runtime uses `on_each_event`: each incoming message is processed independently rather than replaying the entire input buffer.

Pending input is protected against feedback loops. While handling an incoming message, the block updates history without republishing an old user message. A downstream feedback link, such as one from Codex, therefore does not resend the same question indefinitely. Incoming history IDs are derived from the run, source, sequence and payload for stable deduplication.

## Compatibility policy

[compatibility.json](compatibility.json) records HackInvent's verified BloxSmith versions and test evidence. Only the versions listed above have been verified, using the block-owned suites in a **bundled-block test installation**. This is not a certification of managed-package installation, every browser/OS, or live provider availability. Other framework versions are unverified, not necessarily incompatible.

The block-version badge follows `model.json`, not a published Git tag. `unversioned` means that no block release version is declared; no number is inferred from the framework version. The framework still uses `model.json` for its runtime/install contract; the tester-owned JSON does not replace it. Official integration tests run in the private `bloxmith-blocs` workspace. Test helpers and the proprietary framework are not bundled in this public block repository.
