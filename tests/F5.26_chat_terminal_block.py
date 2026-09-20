#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Role: Verifies chat terminal block behavior.
# File Name: F5.26_chat_terminal_block.py
# Author: Alexandre EL
# Email: alex@hackinvent.com
# Created Date: 2026-06-04
# -----------------------------------------------------------------------------

"""F5.26 - Chat terminal block.

The test covers the autonomous Chat terminal block contract: fixed `msg` ports,
modal send action, structured JSON publication, raw/invalid incoming handling,
history persistence, display limit, browser speech-to-text UI action, and
execution in centralized and zeromq_active modes.
"""

# Test cases:
# - FB1 - Publish a pending user message as the stable structured msg payload.
# - FB2 - Parse raw and structured inputs, persist full history, and deduplicate stable message ids.
# - FB3 - Process incoming feedback without republishing the pending user message.
# - FB4 - Render the node card and autonomous modal with safe Markdown and stable scrolling.
# - FB5 - Upload and preserve attachment descriptors in outgoing payloads and history.
# - FB6 - Configure and execute browser-audio STT while masking the API key.
# - FB7 - Exercise outgoing and incoming messages in centralized and zeromq_active runtimes.

from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Any
import tempfile
import sys
import uuid
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blocs.chat_terminal.block import CHAT_DISPLAY_LIMIT, ChatTerminalBlock
from bloxsmith_app.block_runtime import BlockInputEvent, BlockRuntimeContext
from ui_smoke_common import (
    create_project_api,
    create_run_api,
    data_edge,
    display_node,
    expect,
    graph_payload,
    http_json,
    isolated_server,
    prepare_run_api,
    stop_run_api,
    text_node,
    wait_for_run_predicate,
    wait_for_run_terminal,
)
from urllib.parse import quote
from block_test_packages import install_test_package, release_key, surface_payload

SECRET = "sk-chat-terminal-stt-secret"
TRANSCRIPT = "bonjour depuis le micro"


class FakeChatSttHttpServer(ThreadingHTTPServer):
    """Local OpenAI-compatible STT endpoint used by chat terminal tests."""

    requests_log: list[dict[str, Any]]

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), FakeChatSttHandler)
        self.requests_log = []

    @property
    def base_url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"


class FakeChatSttHandler(BaseHTTPRequestHandler):
    """Capture multipart STT requests and return a deterministic transcript."""

    server: FakeChatSttHttpServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        self.server.requests_log.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization") or "",
                "content_type": self.headers.get("Content-Type") or "",
                "body": body,
            }
        )
        response = json.dumps({"text": TRANSCRIPT}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: Any) -> None:
        return


class FakeSttServer:
    """Context manager that starts and stops the fake STT HTTP server."""

    def __enter__(self) -> FakeChatSttHttpServer:
        self.server = FakeChatSttHttpServer()
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)



def upload_attachment_api(server, workspace_project_id: str, graph_id: str, *, filename: str, content: bytes, content_type: str) -> dict:
    """Upload one multipart attachment to the graph-instance endpoint."""

    boundary = f"----cw-chat-attachment-test-{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = Request(
        f"{server.base_url}/api/projects/{workspace_project_id}/graphs/{graph_id}/instances/1/attachments",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def read_binary_url(url: str) -> bytes:
    """Read a binary URL for endpoint regression checks."""

    with urlopen(url, timeout=10) as response:
        return response.read()

def browser_audio_data_url(content: bytes, mime_type: str = "audio/webm") -> str:
    """Return a browser-like MediaRecorder data URL for UI action tests."""

    return f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"



def chat_node(
    node_id: str = "chat-1",
    *,
    pending_message: str = "",
    pending_message_id: str = "",
    history_path: str = "user/projects/chat_terminal_history/{node_id}.jsonl",
) -> dict:
    return {
        "id": node_id,
        "kind": "chat_terminal",
        "title": "Chat terminal",
        "position": {"x": 280, "y": 120},
        "inputs": [
            {
                "id": 1,
                "name": "msg",
                "title": "Msg",
                "accepts": ["message/*", "application/json", "text/plain"],
                "multiplicity": "many",
                "required": False,
            }
        ],
        "outputs": [
            {
                "id": 1,
                "name": "msg",
                "title": "Msg",
                "emits": ["application/json", "message/*"],
                "multiplicity": "many",
            }
        ],
        "config": {
            "pending_message": pending_message,
            "pending_message_id": pending_message_id,
            "pending_message_created_at": 1.0,
            "history_path": history_path,
            "ui_history": [],
        },
    }


def run_node_id_by_kind(run: dict[str, Any], kind: str) -> str:
    """Return the materialized runtime node id for one block kind in a run payload."""

    for node in run.get("graph", {}).get("nodes", []):
        if node.get("kind") == kind:
            return str(node.get("id") or "")
    return ""


def run_result_by_kind(run: dict[str, Any], kind: str) -> tuple[str, dict[str, Any]]:
    """Return one materialized runtime result by block kind for id-remapped runs."""

    node_id = run_node_id_by_kind(run, kind)
    expect(node_id, f"Run node missing for kind {kind}: {run}")
    return node_id, run.get("results", {}).get(node_id, {})


def runtime_context(
    root_dir: Path,
    *,
    node_id: str = "chat-direct",
    pending_message: str = "",
    pending_message_id: str = "",
    input_events: tuple[BlockInputEvent, ...] = (),
    previous_result: dict | None = None,
    pending_attachments: list[dict] | None = None,
) -> BlockRuntimeContext:
    return BlockRuntimeContext(
        run_id="run-direct",
        node_id=node_id,
        kind="chat_terminal",
        title="Chat Direct",
        config={
            "pending_message": pending_message,
            "pending_message_id": pending_message_id,
            "pending_message_created_at": 1.0,
            "pending_attachments": pending_attachments or [],
            "history_path": "user/projects/chat_terminal_history/{node_id}.jsonl",
            "ui_history": [],
        },
        inputs={},
        input_content_types={},
        input_message="",
        input_ports=(SimpleNamespace(id=1, name="msg"),),
        output_ports=(SimpleNamespace(id=1, name="msg"),),
        root_dir=root_dir,
        previous_result=previous_result or {},
        input_events=input_events,
    )


def input_event(value: str, index: int = 1, *, sequence: int | None = None) -> BlockInputEvent:
    return BlockInputEvent(
        edge_id=f"edge-{index}",
        input_port_id=1,
        input_port_name="msg",
        source_node_id=f"source-{index}",
        source_port_id=1,
        value=value,
        content_type="application/json",
        sequence=index if sequence is None else sequence,
    )


def direct_runtime_contract() -> None:
    block = ChatTerminalBlock()
    with tempfile.TemporaryDirectory(prefix="cw-chat-terminal-") as raw_tmp:
        root_dir = Path(raw_tmp)
        first = block.execute_runtime(
            runtime_context(root_dir, pending_message="hello out", pending_message_id="out-direct")
        )
        expect(len(first.outputs) == 1, "A pending chat message must produce one output.")
        output_payload = json.loads(first.outputs[0].value)
        expect(
            output_payload == {"origin_id": "chat-direct", "origin_name": "Chat Direct", "msg": "hello out"},
            f"Unexpected outgoing payload: {output_payload}",
        )


        attachment_descriptor = {
            "id": "att-test",
            "name": "capture.png",
            "mime": "image/png",
            "size": 7,
            "uri": "attachments/att-test/capture.png",
            "url": "/api/projects/1/graphs/graph/instances/1/attachments/att-test/capture.png",
        }
        with_attachment = block.execute_runtime(
            runtime_context(
                root_dir,
                pending_message="analyse image",
                pending_message_id="out-attachment",
                pending_attachments=[attachment_descriptor],
            )
        )
        attachment_payload = json.loads(with_attachment.outputs[0].value)
        expect(
            attachment_payload.get("attachments", [{}])[0].get("name") == "capture.png",
            f"Outgoing chat payload must include attachment descriptors: {attachment_payload}",
        )
        attachment_messages = with_attachment.metadata.get("chat_terminal_display_messages") or []
        expect(
            any(item.get("attachments", [{}])[0].get("id") == "att-test" for item in attachment_messages if item.get("attachments")),
            f"Attachment descriptors must be preserved in history: {attachment_messages}",
        )

        duplicate_pending = block.execute_runtime(
            runtime_context(root_dir, pending_message="hello out", pending_message_id="out-direct")
        )
        expect(len(duplicate_pending.outputs) == 1, "A source execution without incoming feedback may publish the pending message.")
        duplicate_messages = duplicate_pending.metadata.get("chat_terminal_display_messages") or []
        expect(sum(1 for item in duplicate_messages if item.get("message_id") == "out-direct") == 1, "Pending history must stay deduplicated across source executions.")

        feedback_payload = json.dumps({"origin_id": "codex-1", "origin_name": "Codex", "msg": "144"})
        feedback = block.execute_runtime(
            runtime_context(
                root_dir,
                pending_message="hello out",
                pending_message_id="out-direct",
                input_events=(input_event(feedback_payload, 99),),
            )
        )
        expect(len(feedback.outputs) == 0, "Incoming feedback must not republish the last pending chat message.")
        expect("0 publie(s)" in "\n".join(feedback.logs), f"Feedback execution must log zero publication: {feedback.logs}")
        feedback_messages = feedback.metadata.get("chat_terminal_display_messages") or []
        expect(any(item.get("origin_name") == "Codex" and item.get("msg") == "144" for item in feedback_messages), "Feedback message must be kept in history.")
        expect(sum(1 for item in feedback_messages if item.get("message_id") == "out-direct") == 1, "Outgoing message must stay deduplicated.")

        duplicate_feedback = block.execute_runtime(
            runtime_context(
                root_dir,
                input_events=(input_event(feedback_payload, 99),),
            )
        )
        duplicate_feedback_messages = duplicate_feedback.metadata.get("chat_terminal_display_messages") or []
        expect(
            sum(1 for item in duplicate_feedback_messages if item.get("origin_name") == "Codex" and item.get("msg") == "144") == 1,
            "The same incoming active event must not be appended twice.",
        )

        structured = json.dumps({"origin_id": "source-json", "origin_name": "Source JSON", "msg": "structured"})
        second = block.execute_runtime(
            runtime_context(
                root_dir,
                input_events=(
                    input_event(structured, 1),
                    input_event("plain inbound", 2),
                    input_event('{"origin_id":"missing-msg"}', 3),
                ),
            )
        )
        messages = second.metadata.get("chat_terminal_display_messages") or []
        expect(any(item.get("origin_name") == "Source JSON" and item.get("msg") == "structured" for item in messages), "Structured incoming JSON not displayed.")
        expect(any(item.get("origin_name") == "unkbloc" and item.get("msg") == "plain inbound" for item in messages), "Raw incoming text must use unkbloc.")
        expect(any(item.get("origin_name") == "unkbloc" and "missing-msg" in item.get("msg", "") for item in messages), "Unexpected JSON shape must use unkbloc.")

        latest = second
        for index in range(25):
            latest = block.execute_runtime(
                runtime_context(
                    root_dir,
                    input_events=(input_event(json.dumps({"origin_id": "bulk", "origin_name": "Bulk", "msg": f"bulk-{index}"}), index),),
                )
            )
        display_messages = latest.metadata.get("chat_terminal_display_messages") or []
        expect(len(display_messages) == CHAT_DISPLAY_LIMIT, "Display history must be limited to 20 messages.")
        expect(display_messages[0].get("msg") == "bulk-5", f"Unexpected oldest displayed message: {display_messages[0]}")
        history_path = root_dir / "user" / "projects" / "chat_terminal_history" / "chat-direct.jsonl"
        history_lines = history_path.read_text(encoding="utf-8").splitlines()
        expect(len(history_lines) == 31, f"Persistent history must keep all entries, got {len(history_lines)}.")


def outgoing_runtime_document() -> dict:
    return graph_payload(
        "F5 Chat Terminal Outgoing",
        [
            chat_node(pending_message="hello chat", pending_message_id="out-runtime"),
            display_node("display-1", "Display", 560, 120),
        ],
        [data_edge("edge-chat-display", "chat-1", 1, "display-1", 1)],
    )


def incoming_runtime_document(raw_message: str) -> dict:
    return graph_payload(
        "F5 Chat Terminal Incoming",
        [
            text_node("text-1", "Source", raw_message, 80, 120),
            chat_node("chat-1"),
        ],
        [data_edge("edge-text-chat", "text-1", 1, "chat-1", 1)],
    )


def multi_incoming_runtime_document() -> dict:
    """Return an active-runtime graph with two publishers targeting the chat input."""

    return graph_payload(
        "F5 Chat Terminal Multi Incoming",
        [
            text_node("text-1", "Source A", "message A", 80, 120),
            text_node("text-2", "Source B", "message B", 80, 260),
            chat_node("chat-1"),
        ],
        [
            data_edge("edge-text-a-chat", "text-1", 1, "chat-1", 1),
            data_edge("edge-text-b-chat", "text-2", 1, "chat-1", 1),
        ],
    )


def node_card_contract() -> None:
    block = ChatTerminalBlock()
    node = chat_node()
    node["config"]["ui_history"] = [
        {
            "message_id": "preview-1",
            "direction": "in",
            "origin_id": "source",
            "origin_name": "Source",
            "msg": "dernier message visible",
            "raw": "dernier message visible",
            "created_at": 1.0,
        }
    ]
    html = str(block.render_node_card(node=node, payload={}).get("html") or "")
    expect("{{" not in html and "}}" not in html, f"Node card must not leak template placeholders: {html}")
    expect("Chat terminal" in html, f"Node card title missing: {html}")
    expect("dernier message visible" in html, f"Node card preview missing: {html}")
    expect("1 message(s)" in html, f"Node card message count missing: {html}")


def server_contract() -> None:
    with isolated_server() as server:
        # Surfaces are release assets: a bundled kind serves none of them.
        model = install_test_package(server, "chat_terminal")
        key = quote(release_key(model), safe="")
        served = lambda payload, suffix: next(
            asset["path"] for asset in payload["assets"] if asset["path"].endswith(suffix))
        rendered = surface_payload(server, model, chat_node(), "modal")
        modal_html = str(rendered.get("html") or "")
        expect('data-chat-terminal-tab="terminal"' in modal_html, "Chat modal must expose a dedicated Terminal tab.")
        expect('data-chat-terminal-panel="terminal"' in modal_html, "Chat modal must expose the Terminal tab panel.")
        expect('data-chat-terminal-tab="attributes"' in modal_html, "Chat modal must expose a separate Attributes tab.")
        expect('data-chat-terminal-panel="attributes"' in modal_html, "Chat modal must expose the Attributes tab panel.")
        expect('data-chat-terminal-tab="logs"' in modal_html, "Chat modal must expose a block-owned Logs tab.")
        expect('data-chat-terminal-panel="logs"' in modal_html, "Chat modal must expose the Logs tab panel.")
        expect('data-block-modal-error-panel' in modal_html, "Chat logs must own the runtime error panel to suppress the generic Error tab.")
        expect('block-modal-error-tabs' not in modal_html, "Chat modal must not render the generic Erreur tab outside Logs.")
        expect('data-chat-terminal-tab="speech"' not in modal_html, "Speech settings must stay in Attributes, not in a dedicated tab.")
        expect("data-chat-terminal-input" in modal_html, "Chat modal must expose the message input.")
        expect("data-chat-terminal-send" in modal_html, "Chat modal must expose the send button.")
        expect("data-chat-terminal-mic" in modal_html, "Chat modal must expose a microphone button.")
        expect("data-chat-terminal-speech-status" in modal_html, "Chat modal must expose a Speech status line.")
        expect('data-block-config-field="speech_model"' in modal_html, "Speech model must be editable in Attributes.")
        expect('data-block-config-field="speech_api_key"' in modal_html, "Speech API key must be editable in Attributes.")
        expect('data-block-config-field="speech_api_base_url"' in modal_html, "Speech API base URL must be editable in Attributes.")
        expect('data-block-runtime-refresh="autonomous"' in modal_html, "Chat modal must opt into autonomous runtime refresh.")
        expect('data-node-title="Chat terminal"' in modal_html, "Chat modal must expose its title to block-owned JS.")
        expect("data-block-apply" in modal_html, "Chat modal must keep generic title/config apply controls.")
        js_body = (REPO_ROOT / "blocs/chat_terminal/assets/js/block_modal.js").read_text(encoding="utf-8")
        expect("MediaRecorder" in js_body, "Chat modal must use browser MediaRecorder for speech capture.")
        expect('api.applyAction("chat_transcribe_audio"' in js_body, "Chat modal must call the block-owned STT action.")
        expect("sendActiveMessage(root, api, message, attachments)" in js_body, "Active sends must bypass graph patch ui-action.")
        expect('api.blockRequest("remember"' in js_body, "Active sends must persist through block-owned memory.")
        expect("localMessages(root)" in js_body, "Active local messages must survive autonomous history refresh.")
        expect("uploadAttachment(file)" in js_body, "Chat modal must upload files through the attachment endpoint.")
        expect("scrollStreamToBottom(stream)" in js_body, "Chat modal must keep the latest messages visible at the bottom.")
        expect("mountComposerResize(input)" in js_body, "Chat modal must keep the composer compact until text grows.")
        expect("appendRenderedMessage(stream" in js_body, "Chat modal must use backend-rendered Markdown fragments when available.")
        expect("__renderedHtml" in js_body, "Chat modal must carry server-rendered message HTML for optimistic display.")
        css_body = (REPO_ROOT / "blocs/chat_terminal/assets/css/block_modal.css").read_text(encoding="utf-8")
        expect("grid-template-rows: auto auto minmax(0, 1fr)" in css_body, "Chat modal must reserve a bounded scroll area.")
        expect("scrollbar-gutter: stable" in css_body, "Chat stream must keep a stable scroll gutter.")
        expect(".chat-terminal-stream::before" in css_body, "Chat stream must anchor short histories to the bottom.")
        expect(".chat-terminal-attachment-dropzone" in css_body, "Chat modal must style the attachment dropzone.")
        expect(".chat-terminal-markdown pre" in css_body, "Chat modal must style Markdown code blocks.")
        expect(".chat-terminal-logs" in css_body, "Chat modal must style the block-owned Logs tab.")
        assets = rendered.get("assets") or []

        markdown_node = chat_node()
        markdown_source = """## Markdown title

- item **strong**
- [safe](https://example.com/path)

> quoted line

```python
print('<ok>')
```

<script>alert(1)</script>
[bad](javascript:alert(1))"""
        markdown_node["config"]["ui_history"] = [
            {
                "message_id": "markdown-1",
                "direction": "in",
                "origin_id": "source",
                "origin_name": "Source",
                "msg": markdown_source,
                "raw": markdown_source,
                "created_at": 1.0,
            }
        ]
        markdown_rendered = surface_payload(server, model, markdown_node, "modal")
        markdown_html = str(markdown_rendered.get("html") or "")
        expect('class="chat-terminal-text chat-terminal-markdown"' in markdown_html, "Message text must render through the Markdown surface.")
        expect("<h4>Markdown title</h4>" in markdown_html, f"Markdown heading must render safely: {markdown_html}")
        expect("<strong>strong</strong>" in markdown_html, f"Markdown emphasis must render safely: {markdown_html}")
        expect('href="https://example.com/path"' in markdown_html, f"Safe Markdown links must render: {markdown_html}")
        expect("&lt;script&gt;alert(1)&lt;/script&gt;" in markdown_html, "Raw HTML must be escaped in Markdown messages.")
        expect('href="javascript:' not in markdown_html.lower(), "Unsafe Markdown links must not become href attributes.")
        expect("&lt;ok&gt;" in markdown_html, "Fenced code must escape raw code content.")

        attachment_project = create_project_api(
            server,
            title="F5 Chat Terminal Attachments",
            document=graph_payload("F5 Attachments", [chat_node()], []),
        )
        attachment_graph = attachment_project["project"]
        attachment_upload = upload_attachment_api(
            server,
            str(attachment_graph["workspace_project_id"]),
            str(attachment_graph["project_id"]),
            filename="capture.png",
            content=b"png-bytes",
            content_type="image/png",
        )
        descriptor = attachment_upload.get("attachment") or {}
        expect(attachment_upload.get("ok") is True, f"Attachment upload must succeed: {attachment_upload}")
        expect(descriptor.get("name") == "capture.png", f"Attachment descriptor must keep filename: {descriptor}")
        expect(descriptor.get("mime") == "image/png", f"Attachment descriptor must keep MIME type: {descriptor}")
        expect(descriptor.get("scope", {}).get("graph_id") == attachment_graph["project_id"], f"Attachment scope must include graph id: {descriptor}")
        expect(read_binary_url(f"{server.base_url}{descriptor['url']}") == b"png-bytes", "Attachment download endpoint must return stored bytes.")

        secret_node = chat_node()
        secret_node["config"].update({"speech_api_key": SECRET})
        secret_modal = surface_payload(server, model, secret_node, "modal")
        expect(SECRET not in str(secret_modal.get("html") or ""), "Speech API key must not be rendered in modal HTML.")

        generic_patch = http_json(
            server.base_url,
            "/api/blocks/chat_terminal/ui-action",
            method="POST",
            payload={
                "node": chat_node(),
                "action": "modal_update_fields",
                "values": {"node_patch": {"config": {"speech_model": "gpt-4o-mini-transcribe"}}},
            },
        )
        expect(
            generic_patch.get("node_patch", {}).get("config", {}).get("speech_model") == "gpt-4o-mini-transcribe",
            f"Speech config must be persisted through the generic Attributes apply flow: {generic_patch}",
        )

        with FakeSttServer() as fake_stt:
            stt_node = chat_node()
            stt_node["config"].update(
                {
                    "speech_model": "gpt-4o-mini-transcribe",
                    "speech_api_key": SECRET,
                    "speech_api_base_url": fake_stt.base_url,
                    "speech_language": "fr",
                    "speech_prompt": "Conversation courte.",
                    "speech_timeout_sec": 5,
                }
            )
            stt_action = http_json(
                server.base_url,
                "/api/blocks/chat_terminal/ui-action",
                method="POST",
                payload={
                    "node": stt_node,
                    "action": "chat_transcribe_audio",
                    "values": {"data_url": browser_audio_data_url(b"fake browser audio")},
                },
            )
            expect(stt_action.get("transcript") == TRANSCRIPT, f"Speech action must return transcript: {stt_action}")
            expect(SECRET not in json.dumps(stt_action, ensure_ascii=False), "Speech action output must not leak API key.")
            expect(len(fake_stt.requests_log) == 1, "Fake STT endpoint must receive exactly one request.")
            request = fake_stt.requests_log[0]
            body = request["body"]
            expect(request["path"] == "/v1/audio/transcriptions", "Speech action must call OpenAI transcription endpoint.")
            expect(request["authorization"] == f"Bearer {SECRET}", "Speech action must send bearer token to STT endpoint.")
            expect(b'name="model"' in body and b"gpt-4o-mini-transcribe" in body, "Speech action must send configured STT model.")
            expect(b'name="language"' in body and b"fr" in body, "Speech action must send configured STT language.")
            expect(b'name="prompt"' in body and b"Conversation courte." in body, "Speech action must send configured STT prompt.")
            expect(b'name="file"; filename="chat-1-speech.webm"' in body, "Speech action must upload browser audio as a file.")

        previous_openai_key = os.environ.pop("OPENAI_API_KEY", None)
        try:
            missing_key = http_json(
                server.base_url,
                "/api/blocks/chat_terminal/ui-action",
                method="POST",
                payload={
                    "node": chat_node(),
                    "action": "chat_transcribe_audio",
                    "values": {"data_url": browser_audio_data_url(b"fake browser audio")},
                },
            )
        finally:
            if previous_openai_key is not None:
                os.environ["OPENAI_API_KEY"] = previous_openai_key
        expect("api_key" in str(missing_key.get("error") or ""), f"Missing STT key must return a structured error: {missing_key}")

        sent_node = chat_node()
        action = http_json(
            server.base_url,
            "/api/blocks/chat_terminal/ui-action",
            method="POST",
            payload={"node": sent_node, "action": "chat_send_message", "values": {"message": "from modal"}},
        )
        patch_config = action.get("node_patch", {}).get("config", {})
        expect(patch_config.get("pending_message") == "from modal", "Send action must update pending message.")
        expect(action.get("message_entry", {}).get("direction") == "out", "Send action must return the sent entry.")
        expect("data-chat-terminal-markdown" in str(action.get("message_html") or ""), "Send action must return backend-rendered message HTML.")
        markdown_action = http_json(
            server.base_url,
            "/api/blocks/chat_terminal/ui-action",
            method="POST",
            payload={"node": chat_node(), "action": "chat_send_message", "values": {"message": "from **modal** <b>x</b>"}},
        )
        expect(markdown_action.get("message_entry", {}).get("msg") == "from **modal** <b>x</b>", "Markdown send must preserve raw msg text.")
        expect("<strong>modal</strong>" in str(markdown_action.get("message_html") or ""), "Markdown send must render emphasis in message_html.")
        expect("&lt;b&gt;x&lt;/b&gt;" in str(markdown_action.get("message_html") or ""), "Markdown send must escape raw HTML in message_html.")
        sent_node["config"].update(patch_config)
        sent_history = http_json(
            server.base_url,
            "/api/blocks/chat_terminal/history",
            method="POST",
            payload={"node": sent_node, "values": {"limit": 20, "runtime_node_id": "chat-1"}},
        )
        expect(
            "from modal" in str(sent_history.get("messages_html") or ""),
            "History endpoint must preserve the message sent from the modal node preview.",
        )

        history_path = server.root_dir / "user" / "projects" / "chat_terminal_history" / "chat-1.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_entry = {
            "message_id": "persisted-1",
            "direction": "in",
            "origin_id": "source",
            "origin_name": "Source",
            "msg": "persisted from history endpoint",
            "raw": "persisted from history endpoint",
            "created_at": 2.0,
        }
        history_path.write_text(json.dumps(history_entry, ensure_ascii=True) + "\n", encoding="utf-8")
        history = http_json(
            server.base_url,
            "/api/blocks/chat_terminal/history",
            method="POST",
            payload={"node": chat_node(), "values": {"limit": 20, "runtime_node_id": "chat-1"}},
        )
        expect(history.get("message_count") == 1, f"History endpoint must return one persisted message: {history}")
        expect("persisted from history endpoint" in str(history.get("messages_html") or ""), "History endpoint must return rendered message HTML.")
        expect((history.get("messages") or [{}])[0].get("message_id") == "persisted-1", "History endpoint must return normalized messages.")

        remembered_entry = {
            "message_id": "remembered-1",
            "direction": "out",
            "origin_id": "chat-1",
            "origin_name": "Chat terminal",
            "msg": "remembered active send",
            "raw": json.dumps({"origin_id": "chat-1", "origin_name": "Chat terminal", "msg": "remembered active send"}),
            "created_at": 3.0,
        }
        remembered = http_json(
            server.base_url,
            "/api/blocks/chat_terminal/remember",
            method="POST",
            payload={"node": chat_node(), "values": {"limit": 20, "runtime_node_id": "chat-1", "entry": remembered_entry}},
        )
        expect(remembered.get("remembered", {}).get("message_id") == "remembered-1", f"Remember route must persist a normalized entry: {remembered}")
        expect("data-chat-terminal-markdown" in str(remembered.get("message_html") or ""), "Remember route must return backend-rendered message HTML.")
        duplicate = http_json(
            server.base_url,
            "/api/blocks/chat_terminal/remember",
            method="POST",
            payload={"node": chat_node(), "values": {"limit": 20, "runtime_node_id": "chat-1", "entry": remembered_entry}},
        )
        expect(
            sum(1 for item in duplicate.get("messages", []) if item.get("message_id") == "remembered-1") == 1,
            f"Remember route must deduplicate by message_id: {duplicate}",
        )
        expect("remembered active send" in str(duplicate.get("messages_html") or ""), "Remembered messages must be rendered in history.")

        active_document = graph_payload(
            "F5 Chat Terminal Active Publish",
            [chat_node(), display_node("display-1", "Display", 560, 120)],
            [data_edge("edge-chat-display", "chat-1", 1, "display-1", 1)],
        )
        project = create_project_api(server, title="F5 Chat Terminal Active Project", document=active_document)
        project_id = str(project["project"]["project_id"] or "")
        project_state = http_json(server.base_url, f"/api/projects/{project_id}/graph/state")
        active_project_chat_node = next(
            (node for node in project_state.get("document", {}).get("nodes", []) if node.get("kind") == "chat_terminal"),
            {},
        )
        expect(active_project_chat_node.get("id"), f"Active project chat node missing: {project_state}")
        loaded_project_run = http_json(
            server.base_url,
            f"/api/projects/{project_id}/runs/prepare",
            method="POST",
            payload={"runtime_mode": "zeromq_active"},
        )
        loaded_project_run_id = str(loaded_project_run.get("run_id") or "")
        patched_send = http_json(
            server.base_url,
            "/api/blocks/chat_terminal/ui-action",
            method="POST",
            payload={
                "project_id": project_id,
                "base_version": project_state["version"],
                "apply_graph_patch": True,
                "op_id_prefix": "chat-terminal-active-send",
                "node": active_project_chat_node,
                "action": "chat_send_message",
                "values": {"message": "visible pendant event"},
            },
        )
        expect(
            patched_send.get("graph_patch", {}).get("ok") is True,
            f"Chat send action must apply its UI history patch without explicit allow_while_running: {patched_send}",
        )
        expect(
            "visible pendant event" in str(patched_send.get("message_entry", {}).get("msg") or ""),
            f"Chat send action must still return the appended message entry: {patched_send}",
        )
        if loaded_project_run_id:
            stop_run_api(server, loaded_project_run_id)

        prepared = prepare_run_api(server, active_document, runtime_mode="zeromq_active")
        active_run_id = str(prepared.get("run_id") or "")
        active_run_nodes = prepared.get("graph", {}).get("nodes", [])
        active_chat_node_id = str(next((node.get("id") for node in active_run_nodes if node.get("kind") == "chat_terminal"), ""))
        active_display_node_id = str(next((node.get("id") for node in active_run_nodes if node.get("kind") == "display"), ""))
        expect(active_chat_node_id and active_display_node_id, f"Active run nodes missing: {prepared}")
        active_payload = json.dumps(
            {"origin_id": active_chat_node_id, "origin_name": "Chat terminal", "msg": "active modal message"},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        published = http_json(
            server.base_url,
            f"/api/runs/{active_run_id}/active/control",
            method="POST",
            payload={
                "action": "publish_output",
                "node_id": active_chat_node_id,
                "port_name": "msg",
                "value": active_payload,
                "content_type": "application/json",
            },
        )
        published_state = wait_for_run_predicate(
            server,
            active_run_id,
            lambda state: state.get("output_values", {}).get(f"{active_chat_node_id}:1", {}).get("value") == active_payload,
            "Active publish_output must store the chat output value.",
            timeout_sec=5,
        )
        active_state = wait_for_run_predicate(
            server,
            active_run_id,
            lambda state: "active modal message" in str(state.get("results", {}).get(active_display_node_id, {}).get("last_message") or ""),
            "Display must receive a chat message published through active control.",
            timeout_sec=5,
        )
        expect(
            "active modal message" in str(active_state.get("results", {}).get(active_display_node_id, {}).get("last_message") or ""),
            "Display did not receive active published chat payload.",
        )
        stop_run_api(server, active_run_id)

        for runtime_mode in ("centralized", "zeromq_active"):
            created = create_run_api(server, outgoing_runtime_document(), runtime_mode=runtime_mode)
            run = wait_for_run_terminal(server, str(created.get("run_id") or ""))
            expect(run.get("status") == "success", f"Outgoing chat run must succeed in {runtime_mode}.")
            chat_runtime_id, result = run_result_by_kind(run, "chat_terminal")
            output = (result.get("outputs") or {}).get("1") or {}
            payload = json.loads(str(output.get("value") or "{}"))
            expect(payload.get("origin_id") == chat_runtime_id, f"origin_id missing in {runtime_mode}: {payload}")
            expect(payload.get("origin_name") == "Chat terminal", f"origin_name missing in {runtime_mode}: {payload}")
            expect(payload.get("msg") == "hello chat", f"msg missing in {runtime_mode}: {payload}")
            expect(output.get("content_type") == "application/json", f"Output must be JSON in {runtime_mode}.")
            _display_runtime_id, display_result = run_result_by_kind(run, "display")
            expect("hello chat" in str(display_result.get("last_message") or ""), f"Display did not receive chat output in {runtime_mode}.")

        for runtime_mode in ("centralized", "zeromq_active"):
            created = create_run_api(server, incoming_runtime_document("raw terminal input"), runtime_mode=runtime_mode)
            run = wait_for_run_terminal(server, str(created.get("run_id") or ""))
            expect(run.get("status") == "success", f"Incoming chat run must succeed in {runtime_mode}.")
            _chat_runtime_id, result = run_result_by_kind(run, "chat_terminal")
            messages = result.get("chat_terminal_display_messages") or []
            expect(any(item.get("origin_name") == "unkbloc" and item.get("msg") == "raw terminal input" for item in messages), f"Raw input not recorded in {runtime_mode}: {messages}")
            expect((result.get("outputs") or {}) == {}, f"Incoming-only chat must not publish outputs in {runtime_mode}.")
            if runtime_mode == "zeromq_active":
                logs = "\n".join(str(item) for item in run.get("logs", []))
                expect("policy on_each_event" in logs, "Chat terminal must declare the active event policy.")

        created = create_run_api(server, multi_incoming_runtime_document(), runtime_mode="zeromq_active")
        run = wait_for_run_terminal(server, str(created.get("run_id") or ""))
        expect(run.get("status") == "success", "Multi-source incoming chat run must succeed in zeromq_active.")
        chat_runtime_id, result = run_result_by_kind(run, "chat_terminal")
        logs = "\n".join(str(item) for item in run.get("logs", []))
        expect(f"{chat_runtime_id}: received 2 message(s)" not in logs, "Chat terminal must not receive cumulative active batches.")
        messages = result.get("chat_terminal_display_messages") or []
        expect(any(item.get("msg") == "message A" for item in messages), f"Message A missing from active chat history: {messages}")
        expect(any(item.get("msg") == "message B" for item in messages), f"Message B missing from active chat history: {messages}")


def main() -> None:
    direct_runtime_contract()
    node_card_contract()
    server_contract()
    print("[ok] F5.26_chat_terminal_block")


if __name__ == "__main__":
    main()
