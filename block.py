# -----------------------------------------------------------------------------
# Role: Implements the chat terminal block runtime and UI contract.
# File Name: block.py
# Author: Alexandre EL
# Email: alex@hackinvent.com
# Created Date: 2026-06-04
# -----------------------------------------------------------------------------

from __future__ import annotations

import base64
import binascii
from hashlib import sha1
from html import escape
import json
import os
from pathlib import Path
import re
import time
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse
import uuid
from typing import Any

from bloxsmith_app.block_api import (
    APPLICATION_JSON,
    BlockDefinition,
    BlockRuntimeContext,
    BlockRuntimeOutput,
    BlockRuntimeResult,
    configured_user_root_dir,
    normalize_attachment_descriptors,
    render_inspector_template,
    render_node_card_template,
    TEXT_PLAIN,
)


CHAT_DISPLAY_LIMIT = 20
CHAT_WORKER_PREVIEW_LIMIT = 300
UNK_ORIGIN = "unkbloc"
CHAT_STT_MODELS = ("gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1")
DEFAULT_CHAT_STT_MODEL = "gpt-4o-transcribe"
DEFAULT_CHAT_STT_BASE_URL = "https://api.openai.com"
DEFAULT_CHAT_STT_TIMEOUT_SEC = 120
CHAT_STT_RESPONSE_FORMAT = "json"
MAX_CHAT_STT_AUDIO_BYTES = 25 * 1024 * 1024
SUPPORTED_BROWSER_STT_AUDIO_EXTENSIONS = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


class ChatTerminalSpeechError(ValueError):
    """Raised when chat-terminal speech-to-text cannot produce a transcript."""


# Functional behavior:
# FB1 - Publish one structured pending user message on the fixed msg output.
# FB2 - Parse, persist, deduplicate, and expose structured or raw incoming chat messages.
# FB3 - Never republish a pending message while processing incoming feedback.
# FB4 - Render autonomous node-card and modal chat surfaces with safe Markdown.
# FB5 - Preserve uploaded attachment descriptors without embedding binary content.
# FB6 - Transcribe browser audio through the block-owned configurable STT action without exposing secrets.
# FB7 - Keep equivalent message behavior in centralized and zeromq_active runtimes.
class ChatTerminalBlock(BlockDefinition):
    """Autonomous chat terminal block with fixed `msg` input and output ports."""

    kind = "chat_terminal"

    def execute_runtime(self, context: BlockRuntimeContext) -> BlockRuntimeResult:
        """Persist chat history and publish a pending user message once.

        Args:
            context: Runtime context populated by centralized or ZeroMQ active
                execution. Input events are parsed as received chat messages;
                `config.pending_message` is parsed as a user-authored outgoing
                message that may be published on the fixed `msg` output port.

        Returns:
            Runtime result containing one JSON output only when a pending user
            message exists and the current execution is not processing incoming
            feedback, plus metadata for UI history rendering.
        """

        history_path = self._history_path(context)
        history = self._load_history(history_path)
        received_entries = self._received_entries(context)
        outgoing_entry = self._outgoing_entry(context)
        output_entry = outgoing_entry if outgoing_entry and not received_entries else None

        new_entries: list[dict[str, Any]] = []
        candidate_entries: list[dict[str, Any]] = []
        if outgoing_entry:
            candidate_entries.append(outgoing_entry)
        candidate_entries.extend(received_entries)
        for entry in candidate_entries:
            if entry and not self._history_contains(history, entry):
                history.append(entry)
                new_entries.append(entry)
        self._append_history(history_path, new_entries)

        display_messages = history[-CHAT_DISPLAY_LIMIT:]
        output_value = self._outgoing_payload(output_entry) if output_entry else ""
        outputs = []
        if output_entry:
            outputs.append(
                BlockRuntimeOutput(
                    port_id=self._msg_output_port_id(context),
                    port_name="msg",
                    value=output_value,
                    content_type=APPLICATION_JSON,
                    metadata={"message_id": str(output_entry.get("message_id") or "")},
                )
            )
        latest = output_value or str(display_messages[-1].get("msg") if display_messages else "")
        logs = [
            (
                f"[chat_terminal] {context.node_id}: "
                f"{len(received_entries)} recu(s), {1 if output_entry else 0} publie(s), "
                f"historique={self._relative_path(history_path, context.root_dir)}."
            )
        ]
        metadata: dict[str, Any] = {
            "chat_terminal_history_path": self._relative_path(history_path, context.root_dir),
            "chat_terminal_message_count": len(history),
            "chat_terminal_display_messages": display_messages,
        }
        if outgoing_entry:
            metadata["chat_terminal_last_sent_message_id"] = str(outgoing_entry.get("message_id") or "")
        return BlockRuntimeResult(
            status="success",
            outputs=outputs,
            logs=logs,
            last_message=latest,
            content_type=APPLICATION_JSON if output_entry else TEXT_PLAIN,
            worker_received=self._worker_preview(display_messages[-1] if display_messages else None),
            metadata=metadata,
        )

    def render_node_card(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render a compact canvas preview for the chat terminal.

        Args:
            node: Serialized chat terminal node.
            payload: Optional UI payload containing latest runtime metadata.

        Returns:
            Block UI payload used by the generic canvas shell.
        """

        messages = self._modal_messages(node=node, payload=payload or {})
        last_message = messages[-1] if messages else {}
        preview = str(last_message.get("msg") or "Aucun message")
        return render_node_card_template(
            block=self,
            node=node,
            node_classes=["chat-terminal-node"],
            replacements={
                "title": node.get("title") or self.default_title(),
                "preview": self._truncate(preview, 60),
                "length": f"{len(messages)} message(s)",
            },
        )

    def render_inspector_panel(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render a read-only inspector summary for the chat terminal.

        Args:
            node: Serialized chat terminal node.
            payload: Optional UI payload containing latest runtime metadata.

        Returns:
            Block inspector payload with a concise history summary.
        """

        messages = self._modal_messages(node=node, payload=payload or {})
        latest = messages[-1] if messages else {}
        template = (self.directory / "inspector_panel.html").read_text(encoding="utf-8")
        html = render_inspector_template(
            template=template,
            node={**node, "type": self.kind, "kind": self.kind},
            payload=payload,
            replacements={
                "message_count": str(len(messages)),
                "latest_message": escape(str(latest.get("msg") or "Aucun message")),
                "latest_origin": escape(str(latest.get("origin_name") or latest.get("origin_id") or "")),
            },
            show_duplicate=False,
        )
        return {"html": html, "context": {"node_id": str(node.get("id") or ""), "full_panel": True}}

    def render_modal(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the chat terminal modal from block-owned HTML.

        Args:
            node: Serialized chat terminal node.
            payload: Optional UI payload containing latest runtime metadata.

        Returns:
            Modal HTML and context consumed by the shared modal host.
        """

        payload = payload or {}
        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        runtime_node_id = str(runtime.get("node_id") or node.get("id") or "")
        messages = self._modal_messages(node=node, payload=payload)
        template = (self.directory / "block_modal.html").read_text(encoding="utf-8")
        html = (
            template.replace("{{ node_id }}", escape(str(node.get("id") or ""), quote=True))
            .replace("{{ runtime_node_id }}", escape(runtime_node_id, quote=True))
            .replace("{{ node_title }}", escape(str(node.get("title") or self.default_title()), quote=True))
            .replace("{{ node_kind }}", escape(self.kind, quote=True))
            .replace("{{ node_kind_title }}", escape(str(self.model.get("title") or self.default_title()), quote=True))
            .replace("{{ messages_html }}", self._render_messages(messages))
            .replace("{{ message_count }}", str(len(messages)))
            .replace("{{ pending_message }}", escape(self._pending_message_from_node(node)))
            .replace("{{ attributes_html }}", self._render_modal_attributes(node=node, payload=payload))
            .replace("{{ logs_html }}", self._render_generic_modal_error(payload))
        )
        return {
            "html": html,
            "context": {
                "node_id": str(node.get("id") or ""),
                "runtime_node_id": runtime_node_id,
                "node_kind": self.kind,
                "message_count": len(messages),
            },
        }

    def handle_ui_request(
        self,
        *,
        node: dict[str, Any],
        route: str,
        method: str,
        values: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return chat-terminal modal data requested by block-owned JavaScript.

        Args:
            node: Serialized chat terminal node.
            route: Block-owned route; only `history` is currently exposed.
            method: HTTP method used by the modal request.
            values: Request values such as `limit` and `runtime_node_id`.
            payload: Framework context including the internal application root.

        Returns:
            Recent messages and an HTML fragment suitable for the modal stream.
        """

        normalized_route = str(route or "").strip("/")
        if normalized_route not in {"history", "remember"}:
            return {"error": f"unsupported_route:{route}"}
        payload = payload or {}
        root_dir = Path(payload.get("root_dir") or ".")
        runtime_node_id = str(values.get("runtime_node_id") or node.get("id") or "").strip()
        try:
            limit = int(values.get("limit") or CHAT_DISPLAY_LIMIT)
        except (TypeError, ValueError):
            limit = CHAT_DISPLAY_LIMIT
        limit = max(1, min(limit, 100))
        if normalized_route == "remember":
            remembered = self._remember_message_for_node(
                node=node,
                root_dir=root_dir,
                runtime_node_id=runtime_node_id,
                raw_entry=values.get("entry"),
            )
            if remembered is None:
                return {"error": "invalid_memory_entry"}
        messages = self._history_messages_for_node(
            node=node,
            root_dir=root_dir,
            runtime_node_id=runtime_node_id,
            limit=limit,
        )
        return {
            "messages": messages,
            "messages_html": self._render_messages(messages),
            "message_count": len(messages),
            "runtime_node_id": runtime_node_id,
            "remembered": remembered if normalized_route == "remember" else None,
            "message_html": self._render_message(remembered) if normalized_route == "remember" and remembered else None,
        }

    def handle_ui_action(
        self,
        *,
        node: dict[str, Any],
        action: str,
        values: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Handle chat terminal UI actions from the modal.

        Args:
            node: Serialized chat terminal node.
            action: Block-owned action name requested by the frontend.
            values: Action payload. `chat_send_message` expects `message`.
            payload: Optional UI request metadata.

        Returns:
            A node config patch containing the latest pending message and a
            bounded UI history preview.
        """

        if action in {"inspector_update_fields", "modal_update_fields"}:
            return super().handle_ui_action(node=node, action=action, values=values, payload=payload)
        if action == "chat_transcribe_audio":
            return self._handle_speech_transcribe_action(node=node, values=values)
        if action != "chat_send_message":
            return {"error": f"unsupported_action:{action}"}
        message = str(values.get("message") or "").strip()
        attachments = self._normalize_attachments(values.get("attachments"))
        if not message and not attachments:
            return {"error": "empty_message"}
        now = time.time()
        message_id = self._new_message_id("out")
        entry = self._history_entry(
            message_id=message_id,
            direction="out",
            origin_id=str(node.get("id") or self.kind),
            origin_name=str(node.get("title") or self.default_title()),
            msg=message,
            attachments=attachments,
            raw=self._outgoing_payload(
                {
                    "origin_id": str(node.get("id") or self.kind),
                    "origin_name": str(node.get("title") or self.default_title()),
                    "msg": message,
                    "attachments": attachments,
                }
            ),
            created_at=now,
        )
        ui_history = self._ui_history_from_node(node)
        ui_history.append(entry)
        ui_history = ui_history[-CHAT_DISPLAY_LIMIT:]
        return {
            "node_patch": {
                "config": {
                    "pending_message": message,
                    "pending_attachments": attachments,
                    "pending_message_id": message_id,
                    "pending_message_created_at": now,
                    "ui_history": ui_history,
                }
            },
            "message_entry": entry,
            "message_html": self._render_message(entry),
            "output_value": entry["raw"],
            "content_type": APPLICATION_JSON,
            "port_name": "msg",
            "message": "Message chat pret a publier.",
            "allow_while_running": True,
            "rerender_inspector": False,
            "close_modal": False,
        }

    def _handle_speech_transcribe_action(self, *, node: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        """Transcribe a short browser microphone capture for the chat composer.

        Args:
            node: Serialized chat terminal node carrying the STT config.
            values: UI action payload containing a browser audio data URL.

        Returns:
            A transcript payload inserted by the modal JavaScript, or an error.
        """

        config = self._speech_config(node.get("config") if isinstance(node.get("config"), dict) else {})
        try:
            mime_type, audio_bytes = self._decode_speech_data_url(values.get("data_url"))
            response = self._transcribe_speech_bytes(
                audio_bytes=audio_bytes,
                mime_type=mime_type,
                node_id=str(node.get("id") or self.kind),
                config=config,
            )
            transcript = self._speech_transcript_from_response(response).strip()
            if not transcript:
                raise ChatTerminalSpeechError("transcription_vide")
            return {
                "transcript": transcript,
                "model": config["speech_model"],
                "language": config["speech_language"],
                "response": self._redact_speech_response(response, config["speech_api_key"]),
                "message": f"[chat-terminal-stt] {len(transcript)} caractere(s) transcrit(s).",
                "rerender_inspector": False,
                "close_modal": False,
            }
        except ChatTerminalSpeechError as exc:
            return {"error": str(exc), "rerender_inspector": False, "close_modal": False}

    def _render_modal_config_fields(self, node: dict[str, Any]) -> str:
        """Render user-editable chat terminal settings in the Attributes tab."""

        node_config = node.get("config") if isinstance(node.get("config"), dict) else {}
        config = self.default_config()
        config.update(node_config)
        speech = self._speech_config(config, include_env=False)
        api_key_placeholder = "Cle configuree" if str(config.get("speech_api_key") or "").strip() else "sk-..."
        # Keep rare long text fields editable without expanding every Chat Terminal modal by default.
        ergonomic_long_value = str(config.get("ergonomic_long_value") or "")
        ergonomic_long_field = (
            '<div class="field-group"><label>Valeur longue</label>'
            f'<textarea data-block-config-field="ergonomic_long_value" rows="5" spellcheck="false">{escape(ergonomic_long_value)}</textarea>'
            '</div>'
        ) if ergonomic_long_value else ""
        return (
            '<div class="field-group"><label>Chemin historique</label>'
            f'<input data-block-config-field="history_path" type="text" spellcheck="false" value="{escape(str(config.get("history_path") or ""), quote=True)}" />'
            '</div>'
            f'{ergonomic_long_field}'
            '<div class="ports-editor-subsection">'
            '<div class="ports-editor-header"><span class="group-label">Speech to text</span></div>'
            '<div class="field-grid two-cols">'
            '<div class="field-group"><label>Modele STT</label>'
            f'<select data-block-config-field="speech_model">{self._speech_model_options(speech["speech_model"])}</select></div>'
            '<div class="field-group"><label>Timeout sec</label>'
            f'<input data-block-config-field="speech_timeout_sec" data-block-value-type="integer" type="number" min="1" step="1" value="{speech["speech_timeout_sec"]}" /></div>'
            '</div>'
            '<div class="field-group"><label>API base url</label>'
            f'<input data-block-config-field="speech_api_base_url" type="text" autocomplete="off" spellcheck="false" value="{escape(speech["speech_api_base_url"], quote=True)}" /></div>'
            '<div class="field-group"><label>API key</label>'
            f'<input data-block-config-field="speech_api_key" data-block-skip-empty="true" type="password" autocomplete="off" spellcheck="false" placeholder="{escape(api_key_placeholder, quote=True)}" />'
            '<p class="chat-terminal-secret-note">La cle est utilisee uniquement cote serveur et n est pas renvoyee dans les sorties.</p></div>'
            '<div class="field-group"><label>Langue</label>'
            f'<input data-block-config-field="speech_language" type="text" autocomplete="off" spellcheck="false" placeholder="fr" value="{escape(speech["speech_language"], quote=True)}" /></div>'
            '<div class="field-group"><label>Prompt STT</label>'
            f'<textarea data-block-config-field="speech_prompt" rows="3" spellcheck="false" placeholder="Contexte optionnel pour guider la transcription...">{escape(speech["speech_prompt"])}</textarea></div>'
            '</div>'
        )

    def _speech_model_options(self, selected: str) -> str:
        """Return HTML select options for supported chat-terminal STT models."""

        return "\n".join(
            f'<option value="{escape(model, quote=True)}"{" selected" if model == selected else ""}>{escape(model)}</option>'
            for model in CHAT_STT_MODELS
        )

    def _speech_config(self, raw: dict[str, Any] | None, *, include_env: bool = True) -> dict[str, Any]:
        """Normalize chat terminal speech-to-text configuration."""

        config = raw if isinstance(raw, dict) else {}
        model = str(config.get("speech_model") or DEFAULT_CHAT_STT_MODEL).strip()
        if model not in CHAT_STT_MODELS:
            model = DEFAULT_CHAT_STT_MODEL
        api_key = str(config.get("speech_api_key") or "").strip()
        if include_env and not api_key:
            api_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
        return {
            "speech_model": model,
            "speech_api_key": api_key,
            "speech_api_base_url": self._normalize_speech_base_url(config.get("speech_api_base_url")),
            "speech_language": str(config.get("speech_language") or "").strip(),
            "speech_prompt": str(config.get("speech_prompt") or ""),
            "speech_timeout_sec": self._normalize_speech_timeout(config.get("speech_timeout_sec")),
        }

    def _normalize_speech_base_url(self, value: Any) -> str:
        """Normalize the OpenAI-compatible base URL for chat STT."""

        base_url = str(value or DEFAULT_CHAT_STT_BASE_URL).strip().rstrip("/")
        return base_url or DEFAULT_CHAT_STT_BASE_URL

    def _normalize_speech_timeout(self, value: Any) -> int:
        """Normalize chat STT timeout in seconds."""

        try:
            timeout = int(value)
        except (TypeError, ValueError):
            timeout = DEFAULT_CHAT_STT_TIMEOUT_SEC
        return max(1, min(timeout, 3600))

    def _decode_speech_data_url(self, data_url: Any) -> tuple[str, bytes]:
        """Decode a browser MediaRecorder data URL into MIME type and bytes."""

        text = str(data_url or "").strip()
        if "," not in text:
            raise ChatTerminalSpeechError("audio_browser_data_url_invalide")
        header, encoded = text.split(",", 1)
        match = re.fullmatch(
            r"data:(audio/[a-z0-9.+-]+)(?:;codecs=[^;]+)?;base64",
            header.strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            raise ChatTerminalSpeechError("format_audio_navigateur_non_supporte")
        mime_type = match.group(1).lower()
        if mime_type not in SUPPORTED_BROWSER_STT_AUDIO_EXTENSIONS:
            raise ChatTerminalSpeechError("format_audio_navigateur_non_supporte")
        try:
            audio_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ChatTerminalSpeechError("audio_browser_base64_invalide") from exc
        if not audio_bytes:
            raise ChatTerminalSpeechError("audio_browser_vide")
        if len(audio_bytes) > MAX_CHAT_STT_AUDIO_BYTES:
            raise ChatTerminalSpeechError("audio_browser_trop_volumineux")
        return mime_type, audio_bytes

    def _transcribe_speech_bytes(
        self,
        *,
        audio_bytes: bytes,
        mime_type: str,
        node_id: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Call GPT-4o-compatible STT directly from the chat terminal block."""

        api_key = str(config.get("speech_api_key") or "").strip()
        if not api_key:
            raise ChatTerminalSpeechError("api_key OpenAI manquante.")
        fields = {
            "model": str(config["speech_model"]),
            "response_format": CHAT_STT_RESPONSE_FORMAT,
        }
        if config.get("speech_language"):
            fields["language"] = str(config["speech_language"])
        if config.get("speech_prompt"):
            fields["prompt"] = str(config["speech_prompt"])
        filename = f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', node_id or self.kind)}-speech{SUPPORTED_BROWSER_STT_AUDIO_EXTENSIONS[mime_type]}"
        body, content_type = self._speech_multipart_body(
            fields=fields,
            file_field="file",
            filename=filename,
            mime_type=mime_type,
            file_bytes=audio_bytes,
        )
        request = urlrequest.Request(
            self._speech_transcription_endpoint(str(config["speech_api_base_url"])),
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": content_type,
                "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
                "User-Agent": "bloxsmith-chat-terminal-stt/1",
            },
            method="POST",
        )
        try:
            with urlrequest.urlopen(request, timeout=int(config["speech_timeout_sec"])) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                payload = response.read()
                response_content_type = response.headers.get("Content-Type", "")
        except urlerror.HTTPError as exc:
            body_text = exc.read().decode("utf-8", "replace")
            raise ChatTerminalSpeechError(
                f"OpenAI STT HTTP {exc.code}: {self._mask_secret(body_text, api_key)[:600]}"
            ) from exc
        except urlerror.URLError as exc:
            raise ChatTerminalSpeechError(f"OpenAI STT inaccessible: {exc.reason}") from exc
        except OSError as exc:
            raise ChatTerminalSpeechError(f"appel OpenAI STT impossible: {exc}") from exc
        text = payload.decode("utf-8", "replace")
        parsed = self._parse_speech_response_payload(text, content_type=response_content_type)
        parsed.setdefault("status_code", status_code)
        return parsed

    def _speech_transcription_endpoint(self, base_url: str) -> str:
        """Return the OpenAI-compatible audio transcription endpoint."""

        normalized = str(base_url or DEFAULT_CHAT_STT_BASE_URL).rstrip("/")
        if normalized.endswith("/v1"):
            return f"{normalized}/audio/transcriptions"
        return f"{normalized}/v1/audio/transcriptions"

    def _speech_multipart_body(
        self,
        *,
        fields: dict[str, str],
        file_field: str,
        filename: str,
        mime_type: str,
        file_bytes: bytes,
    ) -> tuple[bytes, str]:
        """Build multipart form data for a short browser audio capture."""

        boundary = f"----bloxsmith-chat-terminal-stt-{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                    str(value).encode("utf-8"),
                    b"\r\n",
                ]
            )
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{file_field}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {mime_type or 'application/octet-stream'}\r\n\r\n".encode("utf-8"),
                file_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode("utf-8"),
            ]
        )
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def _parse_speech_response_payload(self, text: str, *, content_type: str) -> dict[str, Any]:
        """Parse GPT-4o-compatible STT response text into a dictionary."""

        if "json" not in str(content_type or "").lower():
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
            return parsed if isinstance(parsed, dict) else {"value": parsed, "text": text}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
        return parsed if isinstance(parsed, dict) else {"value": parsed, "text": text}

    def _speech_transcript_from_response(self, response: dict[str, Any]) -> str:
        """Extract transcript text from common OpenAI-compatible STT response shapes."""

        for key in ("text", "transcript", "content"):
            value = response.get(key)
            if isinstance(value, str):
                return value
        segments = response.get("segments")
        if isinstance(segments, list):
            parts = [str(item.get("text") or "").strip() for item in segments if isinstance(item, dict)]
            joined = "\n".join(part for part in parts if part)
            if joined:
                return joined
        return json.dumps(response, ensure_ascii=False)

    def _redact_speech_response(self, response: dict[str, Any], api_key: str) -> dict[str, Any]:
        """Return a JSON-safe STT response copy with sensitive values masked."""

        text = json.dumps(response, ensure_ascii=False, default=str)
        redacted = self._mask_secret(text, api_key)
        try:
            parsed = json.loads(redacted)
        except json.JSONDecodeError:
            return {"text": redacted}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def _mask_secret(self, text: str, secret: str) -> str:
        """Mask one sensitive secret from error messages and debug payloads."""

        if not secret:
            return str(text or "")
        return str(text or "").replace(secret, "[redacted]")

    def _received_entries(self, context: BlockRuntimeContext) -> list[dict[str, Any]]:
        """Parse runtime input events into persisted chat history entries."""

        entries: list[dict[str, Any]] = []
        events = tuple(getattr(context, "input_events", ()) or ())
        for event in events:
            raw = str(getattr(event, "value", "") or "")
            if raw:
                entries.append(
                    self._entry_from_raw(
                        raw,
                        direction="in",
                        message_id=self._incoming_event_message_id(context, event, raw),
                    )
                )
        if entries:
            return entries
        raw_input = str((getattr(context, "inputs", {}) or {}).get("msg") or (getattr(context, "inputs", {}) or {}).get("1") or "")
        if raw_input:
            entries.append(
                self._entry_from_raw(
                    raw_input,
                    direction="in",
                    message_id=self._incoming_raw_message_id(context, raw_input),
                )
            )
        return entries

    def _entry_from_raw(self, raw: str, *, direction: str, message_id: str = "") -> dict[str, Any]:
        """Convert a raw incoming payload into a normalized history entry."""

        parsed = self._parse_chat_payload(raw)
        return self._history_entry(
            message_id=message_id or self._new_message_id("in"),
            direction=direction,
            origin_id=parsed["origin_id"],
            origin_name=parsed["origin_name"],
            msg=parsed["msg"],
            attachments=parsed.get("attachments") or [],
            raw=raw,
            created_at=time.time(),
        )

    def _incoming_event_message_id(self, context: BlockRuntimeContext, event: Any, raw: str) -> str:
        """Return a stable local id for one incoming active-runtime event."""

        identity = {
            "run_id": str(getattr(context, "run_id", "") or ""),
            "edge_id": str(getattr(event, "edge_id", "") or ""),
            "input_port_id": int(getattr(event, "input_port_id", 0) or 0),
            "source_node_id": str(getattr(event, "source_node_id", "") or ""),
            "source_port_id": int(getattr(event, "source_port_id", 0) or 0),
            "sequence": int(getattr(event, "sequence", 0) or 0),
            "raw": str(raw or ""),
        }
        payload = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return f"in-{sha1(payload.encode('utf-8')).hexdigest()[:16]}"

    def _incoming_raw_message_id(self, context: BlockRuntimeContext, raw: str) -> str:
        """Return a stable local id for a raw centralized input value."""

        identity = {
            "run_id": str(getattr(context, "run_id", "") or ""),
            "node_id": str(getattr(context, "node_id", "") or ""),
            "raw": str(raw or ""),
        }
        payload = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return f"in-{sha1(payload.encode('utf-8')).hexdigest()[:16]}"

    def _parse_chat_payload(self, raw: str) -> dict[str, Any]:
        """Parse a structured JSON chat payload or return an `unkbloc` fallback.

        Args:
            raw: Incoming runtime payload.

        Returns:
            A mapping with `origin_id`, `origin_name`, and `msg`.
        """

        text = str(raw or "")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"origin_id": UNK_ORIGIN, "origin_name": UNK_ORIGIN, "msg": text}
        if not isinstance(payload, dict) or ("msg" not in payload and "attachments" not in payload):
            return {"origin_id": UNK_ORIGIN, "origin_name": UNK_ORIGIN, "msg": text, "attachments": []}
        origin_id = str(payload.get("origin_id") or UNK_ORIGIN).strip() or UNK_ORIGIN
        origin_name = str(payload.get("origin_name") or origin_id).strip() or origin_id
        return {
            "origin_id": origin_id,
            "origin_name": origin_name,
            "msg": "" if payload.get("msg") is None else str(payload.get("msg")),
            "attachments": self._normalize_attachments(payload.get("attachments")),
        }

    def _outgoing_entry(self, context: BlockRuntimeContext) -> dict[str, Any] | None:
        """Return the pending user-authored message as a history entry."""

        message = str((getattr(context, "config", {}) or {}).get("pending_message") or "").strip()
        if not message:
            return None
        message_id = str((getattr(context, "config", {}) or {}).get("pending_message_id") or "").strip()
        if not message_id:
            message_id = self._stable_pending_id(context.node_id, message)
        created_at = self._float_config(context, "pending_message_created_at") or time.time()
        attachments = self._normalize_attachments((getattr(context, "config", {}) or {}).get("pending_attachments"))
        return self._history_entry(
            message_id=message_id,
            direction="out",
            origin_id=str(context.node_id or self.kind),
            origin_name=str(context.title or self.default_title()),
            msg=message,
            attachments=attachments,
            raw=self._outgoing_payload({"origin_id": context.node_id, "origin_name": context.title, "msg": message, "attachments": attachments}),
            created_at=created_at,
        )

    def _outgoing_payload(self, entry: dict[str, Any]) -> str:
        """Serialize a user-authored chat message for the output `msg` port."""

        payload = {
            "origin_id": str(entry.get("origin_id") or ""),
            "origin_name": str(entry.get("origin_name") or ""),
            "msg": str(entry.get("msg") or ""),
        }
        attachments = self._normalize_attachments(entry.get("attachments"))
        if attachments:
            payload["attachments"] = attachments
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

    def _history_entry(
        self,
        *,
        message_id: str,
        direction: str,
        origin_id: str,
        origin_name: str,
        msg: str,
        raw: str,
        created_at: float,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build one normalized history record stored by the chat terminal."""

        record = {
            "message_id": str(message_id or self._new_message_id("msg")),
            "direction": str(direction or "in"),
            "origin_id": str(origin_id or UNK_ORIGIN),
            "origin_name": str(origin_name or origin_id or UNK_ORIGIN),
            "msg": str(msg or ""),
            "raw": str(raw or ""),
            "created_at": float(created_at or time.time()),
        }
        normalized_attachments = self._normalize_attachments(attachments)
        if normalized_attachments:
            record["attachments"] = normalized_attachments
        return record

    def _history_path(self, context: BlockRuntimeContext) -> Path:
        """Resolve the chat history JSONL path inside the runtime root."""

        return self._resolve_history_path(
            root_dir=Path(context.root_dir),
            config=getattr(context, "config", {}) or {},
            node_id=str(context.node_id or self.kind),
            run_id=str(context.run_id or ""),
        )

    def _resolve_history_path(
        self,
        *,
        root_dir: Path,
        config: dict[str, Any],
        node_id: str,
        run_id: str = "",
    ) -> Path:
        """Resolve a configured history path while keeping it inside root_dir.

        Args:
            root_dir: Runtime/application root that bounds file access.
            config: Node configuration containing optional `history_path`.
            node_id: Runtime node id used for `{node_id}` substitution.
            run_id: Run id used for `{run_id}` substitution.
        """

        raw = str((config or {}).get("history_path") or "").strip()
        root = Path(root_dir).resolve()
        safe_node_id = self._safe_path_part(node_id)
        default_pattern = "user/projects/chat_terminal_history/{node_id}.jsonl"
        if not raw or raw == default_pattern:
            return configured_user_root_dir(root) / "projects" / "chat_terminal_history" / f"{safe_node_id}.jsonl"

        replacements = {
            "{node_id}": safe_node_id,
            "{run_id}": self._safe_path_part(run_id),
        }
        for token, value in replacements.items():
            raw = raw.replace(token, value)
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = root / path
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            resolved = configured_user_root_dir(root) / "projects" / "chat_terminal_history" / f"{safe_node_id}.jsonl"
        return resolved

    def _load_history(self, path: Path) -> list[dict[str, Any]]:
        """Load the complete JSONL history file, ignoring malformed lines."""

        if not path.is_file():
            return []
        messages: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            normalized = self._normalize_history_record(raw)
            if normalized:
                messages.append(normalized)
        return messages

    def _append_history(self, path: Path, entries: list[dict[str, Any]]) -> None:
        """Append new entries to the persistent JSONL history file."""

        if not entries:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n")

    def _normalize_history_record(self, raw: Any) -> dict[str, Any] | None:
        """Return a normalized persisted history record or None."""

        if not isinstance(raw, dict):
            return None
        return self._history_entry(
            message_id=str(raw.get("message_id") or raw.get("id") or self._new_message_id("msg")),
            direction=str(raw.get("direction") or "in"),
            origin_id=str(raw.get("origin_id") or UNK_ORIGIN),
            origin_name=str(raw.get("origin_name") or raw.get("origin_id") or UNK_ORIGIN),
            msg=str(raw.get("msg") or raw.get("message") or ""),
            attachments=self._normalize_attachments(raw.get("attachments")),
            raw=str(raw.get("raw") or ""),
            created_at=self._safe_float(raw.get("created_at")) or time.time(),
        )

    def _history_contains(self, history: list[dict[str, Any]], entry: dict[str, Any]) -> bool:
        """Return whether a history list already contains the entry message id."""

        message_id = str(entry.get("message_id") or "")
        return bool(message_id) and any(str(item.get("message_id") or "") == message_id for item in history)

    def _modal_messages(self, *, node: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Resolve the bounded modal history from node config and runtime payload."""

        return self._merge_history_sources(
            self._runtime_messages(payload),
            self._ui_history_from_node(node),
            limit=CHAT_DISPLAY_LIMIT,
        )

    def _history_messages_for_node(
        self,
        *,
        node: dict[str, Any],
        root_dir: Path,
        runtime_node_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Load recent modal messages from persistent history and node UI preview.

        Args:
            node: Serialized node whose config owns the history path template.
            root_dir: Application root bounding history file access.
            runtime_node_id: Runtime node id used for default history naming.
            limit: Maximum messages returned to the modal.
        """

        history_path = self._history_path_for_node(
            node=node,
            root_dir=root_dir,
            runtime_node_id=runtime_node_id,
        )
        return self._merge_history_sources(
            self._load_history(history_path),
            self._ui_history_from_node(node),
            limit=limit,
        )

    def _remember_message_for_node(
        self,
        *,
        node: dict[str, Any],
        root_dir: Path,
        runtime_node_id: str,
        raw_entry: Any,
    ) -> dict[str, Any] | None:
        """Persist one UI-authored message into the block-owned memory.

        Args:
            node: Serialized chat terminal node.
            root_dir: Application root bounding the memory file.
            runtime_node_id: Runtime node id used by the active run.
            raw_entry: Candidate message entry sent by block-owned JavaScript.

        Returns:
            The normalized entry when it was valid, otherwise None. Existing
            message ids are deduplicated, so replays keep memory stable.
        """

        entry = self._normalize_history_record(raw_entry)
        if not entry:
            return None
        history_path = self._history_path_for_node(
            node=node,
            root_dir=root_dir,
            runtime_node_id=runtime_node_id,
        )
        history = self._load_history(history_path)
        if not self._history_contains(history, entry):
            self._append_history(history_path, [entry])
        return entry

    def _history_path_for_node(self, *, node: dict[str, Any], root_dir: Path, runtime_node_id: str) -> Path:
        """Resolve the block-owned memory path for modal requests."""

        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        return self._resolve_history_path(
            root_dir=root_dir,
            config=config,
            node_id=runtime_node_id or str(node.get("id") or self.kind),
        )

    def _merge_history_sources(self, *sources: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        """Normalize, deduplicate, sort, and bound several history sources."""

        messages: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in sources:
            for item in source or []:
                normalized = self._normalize_history_record(item)
                if not normalized:
                    continue
                message_id = str(normalized.get("message_id") or "")
                if message_id and message_id in seen:
                    continue
                if message_id:
                    seen.add(message_id)
                messages.append(normalized)
        messages.sort(key=lambda item: float(item.get("created_at") or 0.0))
        return messages[-max(1, int(limit or CHAT_DISPLAY_LIMIT)):]

    def _runtime_messages(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract chat terminal messages from generic runtime UI payload."""

        runtime = payload.get("runtime") if isinstance(payload, dict) else None
        if not isinstance(runtime, dict):
            return []
        candidates = [runtime.get("result"), runtime.get("worker_row"), runtime]
        for candidate in candidates:
            if isinstance(candidate, dict) and isinstance(candidate.get("chat_terminal_display_messages"), list):
                return list(candidate.get("chat_terminal_display_messages") or [])
        return []

    def _ui_history_from_node(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the recent UI history preview stored in node config."""

        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        raw_history = config.get("ui_history") if isinstance(config, dict) else []
        return list(raw_history) if isinstance(raw_history, list) else []

    def _render_messages(self, messages: list[dict[str, Any]]) -> str:
        """Render chronological chat messages for the modal."""

        if not messages:
            return '<div class="chat-terminal-empty" data-chat-terminal-empty>Aucun message.</div>'
        return "\n".join(self._render_message(item) for item in messages[-CHAT_DISPLAY_LIMIT:])

    def _render_message(self, item: dict[str, Any]) -> str:
        """Render one chat message with safe Markdown visualization for its text."""

        direction = "out" if str(item.get("direction") or "") == "out" else "in"
        origin = str(item.get("origin_name") or item.get("origin_id") or UNK_ORIGIN)
        message_id = str(item.get("message_id") or "")
        msg = str(item.get("msg") or "")
        direction_label = "sortant" if direction == "out" else "entrant"
        return (
            f'<article class="chat-terminal-message is-{escape(direction, quote=True)}" data-chat-terminal-message '
            f'data-chat-terminal-message-id="{escape(message_id, quote=True)}">'
            '<header>'
            f'<span class="chat-terminal-origin">{escape(origin)}</span>'
            '<details class="chat-terminal-message-meta">'
            f'<summary>{escape(direction_label)}</summary>'
            f'<span class="chat-terminal-id">{escape(message_id)}</span>'
            '</details>'
            '</header>'
            f'{self._render_message_text(msg)}'
            f'{self._render_message_attachments(item)}'
            '</article>'
        )

    def _render_message_text(self, message: str) -> str:
        """Render the message body as safe Markdown while keeping storage raw."""

        return (
            '<div class="chat-terminal-text chat-terminal-markdown" data-chat-terminal-markdown>'
            f'{self._render_markdown(message)}'
            '</div>'
        )

    def _render_markdown(self, text: str) -> str:
        """Render a conservative Markdown subset with escaped raw HTML."""

        lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        blocks: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if not stripped:
                index += 1
                continue

            fence = re.match(r"^\s*```([A-Za-z0-9_.+-]*)\s*$", line)
            if fence:
                language = re.sub(r"[^A-Za-z0-9_.+-]+", "", fence.group(1) or "")[:32]
                index += 1
                code_lines: list[str] = []
                while index < len(lines) and not re.match(r"^\s*```\s*$", lines[index]):
                    code_lines.append(lines[index])
                    index += 1
                if index < len(lines):
                    index += 1
                class_attr = f' class="language-{escape(language, quote=True)}"' if language else ""
                blocks.append(f"<pre><code{class_attr}>{escape(chr(10).join(code_lines))}</code></pre>")
                continue

            heading = re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", stripped)
            if heading:
                level = 2 + len(heading.group(1))
                blocks.append(f"<h{level}>{self._render_markdown_inline(heading.group(2))}</h{level}>")
                index += 1
                continue

            if re.match(r"^\s{0,3}>\s?", line):
                quote_lines: list[str] = []
                while index < len(lines):
                    quote = re.match(r"^\s{0,3}>\s?(.*)$", lines[index])
                    if not quote:
                        break
                    quote_lines.append(quote.group(1))
                    index += 1
                blocks.append(f"<blockquote>{self._render_markdown_paragraph(quote_lines)}</blockquote>")
                continue

            if self._is_markdown_table_start(lines, index):
                table_html, index = self._render_markdown_table(lines, index)
                blocks.append(table_html)
                continue

            unordered = re.match(r"^\s{0,3}[-*+]\s+(.+)$", line)
            ordered = re.match(r"^\s{0,3}\d+[.)]\s+(.+)$", line)
            if unordered or ordered:
                tag = "ul" if unordered else "ol"
                items: list[str] = []
                while index < len(lines):
                    match = re.match(r"^\s{0,3}[-*+]\s+(.+)$", lines[index]) if tag == "ul" else re.match(r"^\s{0,3}\d+[.)]\s+(.+)$", lines[index])
                    if not match:
                        break
                    items.append(f"<li>{self._render_markdown_inline(match.group(1))}</li>")
                    index += 1
                blocks.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
                continue

            paragraph_lines: list[str] = []
            while index < len(lines) and lines[index].strip() and not self._is_markdown_block_start(lines, index):
                paragraph_lines.append(lines[index])
                index += 1
            if paragraph_lines:
                blocks.append(self._render_markdown_paragraph(paragraph_lines))
            else:
                index += 1
        return "\n".join(blocks) if blocks else "<p></p>"

    def _is_markdown_block_start(self, lines: list[str], index: int) -> bool:
        """Return whether the line begins a Markdown block outside a paragraph."""

        if index < 0 or index >= len(lines):
            return False
        line = lines[index]
        stripped = line.strip()
        return bool(
            re.match(r"^\s*```", line)
            or re.match(r"^#{1,3}\s+", stripped)
            or re.match(r"^\s{0,3}>\s?", line)
            or re.match(r"^\s{0,3}[-*+]\s+", line)
            or re.match(r"^\s{0,3}\d+[.)]\s+", line)
            or self._is_markdown_table_start(lines, index)
        )

    def _render_markdown_paragraph(self, lines: list[str]) -> str:
        """Render paragraph lines while preserving explicit line breaks."""

        rendered = "<br>".join(self._render_markdown_inline(line) for line in lines)
        return f"<p>{rendered}</p>"

    def _is_markdown_table_start(self, lines: list[str], index: int) -> bool:
        """Return whether the current and next line form a simple Markdown table."""

        if index + 1 >= len(lines) or "|" not in lines[index]:
            return False
        separator = lines[index + 1].strip().strip("|")
        if not separator:
            return False
        cells = [cell.strip() for cell in separator.split("|")]
        return len(cells) >= 2 and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)

    def _render_markdown_table(self, lines: list[str], index: int) -> tuple[str, int]:
        """Render a simple pipe table and return the next unread line index."""

        headers = self._split_markdown_table_row(lines[index])
        index += 2
        rows: list[list[str]] = []
        while index < len(lines) and lines[index].strip() and "|" in lines[index]:
            rows.append(self._split_markdown_table_row(lines[index]))
            index += 1
        width = len(headers)
        header_html = "".join(f"<th>{self._render_markdown_inline(cell)}</th>" for cell in headers)
        body_rows = []
        for row in rows:
            cells = (row + [""] * width)[:width]
            body_rows.append("<tr>" + "".join(f"<td>{self._render_markdown_inline(cell)}</td>" for cell in cells) + "</tr>")
        return "<table><thead><tr>" + header_html + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>", index

    def _split_markdown_table_row(self, line: str) -> list[str]:
        """Split a basic pipe table row into trimmed cells."""

        return [cell.strip() for cell in str(line or "").strip().strip("|").split("|")]

    def _render_markdown_inline(self, text: str) -> str:
        """Render safe inline Markdown for emphasis, code, and whitelisted links."""

        source = str(text or "")
        parts: list[str] = []
        plain: list[str] = []

        def flush_plain() -> None:
            if plain:
                parts.append(self._render_markdown_emphasis("".join(plain)))
                plain.clear()

        index = 0
        while index < len(source):
            char = source[index]
            if char == "`":
                end = source.find("`", index + 1)
                if end != -1:
                    flush_plain()
                    parts.append(f"<code>{escape(source[index + 1:end])}</code>")
                    index = end + 1
                    continue
            if char == "[":
                label_end = source.find("](", index + 1)
                href_end = source.find(")", label_end + 2) if label_end != -1 else -1
                if label_end != -1 and href_end != -1:
                    href = self._safe_markdown_href(source[label_end + 2:href_end])
                    if href:
                        flush_plain()
                        label = self._render_markdown_emphasis(source[index + 1:label_end])
                        parts.append(
                            f'<a href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">{label}</a>'
                        )
                        index = href_end + 1
                        continue
            plain.append(char)
            index += 1
        flush_plain()
        return "".join(parts)

    def _render_markdown_emphasis(self, text: str) -> str:
        """Render emphasis markers inside text that has no HTML privileges."""

        html = escape(str(text or ""))
        html = re.sub(r"\*\*([^*\n]+?)\*\*", r"<strong>\1</strong>", html)
        html = re.sub(r"__([^_\n]+?)__", r"<strong>\1</strong>", html)
        html = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", html)
        html = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<em>\1</em>", html)
        return html

    def _safe_markdown_href(self, raw_href: str) -> str:
        """Return a link href only for explicitly supported URL schemes."""

        href = str(raw_href or "").strip()
        if href.startswith("<") and href.endswith(">"):
            href = href[1:-1].strip()
        if not href or any(char in href for char in "\x00\r\n"):
            return ""
        parsed = urlparse(href)
        return href if parsed.scheme.lower() in {"http", "https", "mailto"} else ""

    def _render_modal_attributes(self, *, node: dict[str, Any], payload: dict[str, Any]) -> str:
        """Render generic identity, config, ports, and runtime sections."""

        title = escape(str(node.get("title") or self.default_title()), quote=True)
        return (
            '<section class="ports-editor-section">'
            '<div class="ports-editor-header"><span class="group-label">Identite</span></div>'
            '<div class="field-group">'
            '<label>Nom du bloc</label>'
            f'<input data-block-title-field type="text" autocomplete="off" value="{title}" />'
            '</div>'
            '<div class="inspector-actions"><button class="solid-btn" data-block-apply type="button" disabled>Appliquer</button></div>'
            '</section>'
            '<section class="ports-editor-section">'
            '<div class="ports-editor-header"><span class="group-label">Configuration</span></div>'
            f'{self._render_modal_config_fields(node)}'
            '</section>'
            '<section class="ports-editor-section">'
            '<div class="ports-editor-header"><span class="group-label">Ports</span></div>'
            f'{self._render_generic_modal_ports(node)}'
            '</section>'
            '<section class="ports-editor-section">'
            '<div class="ports-editor-header"><span class="group-label">Dernier etat</span></div>'
            f'{self._render_generic_modal_runtime(payload)}'
            '</section>'
        )

    def _pending_message_from_node(self, node: dict[str, Any]) -> str:
        """Return the current pending message from node config."""

        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        return str(config.get("pending_message") or "") if isinstance(config, dict) else ""


    def _normalize_attachments(self, raw: Any) -> list[dict[str, Any]]:
        """Return bounded attachment descriptors safe for history/runtime JSON."""

        return normalize_attachment_descriptors(raw, limit=10)

    def _render_message_attachments(self, entry: dict[str, Any]) -> str:
        """Render attachment links for one chat history message."""

        attachments = self._normalize_attachments(entry.get("attachments"))
        if not attachments:
            return ""
        chips = []
        for item in attachments:
            label = escape(str(item.get("name") or "attachment"))
            href = escape(str(item.get("url") or ""), quote=True)
            size = self._format_attachment_size(self._safe_int(item.get("size")))
            suffix = f'<small>{escape(size)}</small>' if size else ""
            if href:
                chips.append(f'<a href="{href}" target="_blank" rel="noopener" class="chat-terminal-attachment">{label}{suffix}</a>')
            else:
                chips.append(f'<span class="chat-terminal-attachment">{label}{suffix}</span>')
        return '<div class="chat-terminal-attachments">' + "".join(chips) + "</div>"

    def _format_attachment_size(self, size: int) -> str:
        """Return a compact human-readable attachment size."""

        if size <= 0:
            return ""
        if size < 1024:
            return f"{size} o"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} Ko"
        return f"{size / (1024 * 1024):.1f} Mo"

    def _safe_int(self, value: Any) -> int:
        """Convert a value to a non-negative int or return zero."""

        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def _worker_preview(self, entry: dict[str, Any] | None) -> str:
        """Return a bounded worker row preview for the latest chat message."""

        if not entry:
            return ""
        attachments = self._normalize_attachments(entry.get("attachments"))
        suffix = f" ({len(attachments)} piece(s) jointe(s))" if attachments else ""
        text = f"{entry.get('origin_name') or entry.get('origin_id') or UNK_ORIGIN}: {entry.get('msg') or ''}{suffix}"
        return self._truncate(text, CHAT_WORKER_PREVIEW_LIMIT)

    def _truncate(self, value: str, max_length: int) -> str:
        """Return a compact one-line preview."""

        text = str(value or "").replace("\n", " ").strip()
        return text if len(text) <= max_length else f"{text[: max_length - 1]}..."

    def _msg_output_port_id(self, context: BlockRuntimeContext) -> int:
        """Return the fixed `msg` output port id, defaulting to 1."""

        for port in getattr(context, "output_ports", ()) or ():
            if str(getattr(port, "name", "") or "") == "msg":
                return int(getattr(port, "id", 1) or 1)
        return 1

    def _relative_path(self, path: Path, root: Path) -> str:
        """Return a readable path relative to root when possible."""

        try:
            return str(path.resolve().relative_to(Path(root).resolve()))
        except ValueError:
            return str(path)

    def _stable_pending_id(self, node_id: str, message: str) -> str:
        """Return a deterministic id for manually configured pending messages."""

        digest = sha1(f"{node_id}\0{message}".encode("utf-8")).hexdigest()[:12]
        return f"out-{digest}"

    def _new_message_id(self, prefix: str) -> str:
        """Return a locally unique, displayable message identifier."""

        return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"

    def _safe_path_part(self, value: str) -> str:
        """Return a filesystem-safe path segment."""

        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._-")
        return normalized or "chat_terminal"

    def _float_config(self, context: BlockRuntimeContext, key: str) -> float:
        """Read one float config value from the runtime context."""

        return self._safe_float((getattr(context, "config", {}) or {}).get(key))

    def _safe_float(self, value: Any) -> float:
        """Convert a value to float or return 0.0."""

        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
