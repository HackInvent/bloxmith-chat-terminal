/**
 * Return a stable message id from a normalized chat entry.
 *
 * @param {Object} entry - Chat message entry.
 * @returns {string} Message id or an empty string.
 */
function messageId(entry) {
  return String(entry?.message_id || entry?.id || "").trim();
}

/**
 * Escape an attribute value for a querySelector exact match.
 *
 * @param {string} value - Raw attribute value.
 * @returns {string} CSS selector-safe value.
 */
function cssEscape(value) {
  if (window.CSS?.escape) {
    return window.CSS.escape(value);
  }
  return String(value || "").replace(/[\"\\]/g, "\\$&");
}

/**
 * Track optimistic messages sent while an active runtime is loaded.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @returns {Object[]} Mutable list of local messages.
 */
function localMessages(root) {
  if (!Array.isArray(root.__cwChatTerminalLocalMessages)) {
    root.__cwChatTerminalLocalMessages = [];
  }
  return root.__cwChatTerminalLocalMessages;
}

const scrollBottomThreshold = 24;

/**
 * Keep the latest messages visible after DOM or layout updates.
 *
 * @param {HTMLElement} stream - Message stream container.
 */
function scrollStreamToBottom(stream) {
  if (!stream) {
    return;
  }
  stream.scrollTop = stream.scrollHeight;
  window.requestAnimationFrame?.(() => {
    stream.scrollTop = stream.scrollHeight;
  });
}

/**
 * Return whether the reader is already looking at the newest messages.
 *
 * @param {HTMLElement} stream - Message stream container.
 * @returns {boolean} True when close enough to the bottom.
 */
function isNearBottom(stream) {
  if (!stream) {
    return false;
  }
  return stream.scrollTop + stream.clientHeight >= stream.scrollHeight - scrollBottomThreshold;
}

/**
 * Capture the visible message used to keep reading position stable.
 *
 * @param {HTMLElement} stream - Message stream container.
 * @returns {Object} Scroll anchor to restore after history refresh.
 */
function captureScrollAnchor(stream) {
  if (!stream) {
    return { mode: "position", scrollTop: 0 };
  }
  if (isNearBottom(stream)) {
    return { mode: "bottom" };
  }
  const streamRect = stream.getBoundingClientRect();
  const messages = Array.from(stream.querySelectorAll("[data-chat-terminal-message-id]"));
  for (const message of messages) {
    const rect = message.getBoundingClientRect();
    if (rect.bottom > streamRect.top && rect.top < streamRect.bottom) {
      return {
        mode: "message",
        id: message.dataset.chatTerminalMessageId || "",
        topOffset: rect.top - streamRect.top,
        scrollTop: stream.scrollTop,
      };
    }
  }
  return { mode: "position", scrollTop: stream.scrollTop };
}

/**
 * Restore a previously captured reading anchor after DOM replacement.
 *
 * @param {HTMLElement} stream - Message stream container.
 * @param {Object} anchor - Anchor returned by captureScrollAnchor.
 * @returns {boolean} True when the stream should remain stuck to the bottom.
 */
function restoreScrollAnchor(stream, anchor) {
  if (!stream || !anchor) {
    return false;
  }
  if (anchor.mode === "bottom") {
    scrollStreamToBottom(stream);
    return true;
  }
  if (anchor.mode === "message" && anchor.id) {
    const message = stream.querySelector(`[data-chat-terminal-message-id="${cssEscape(anchor.id)}"]`);
    if (message) {
      const streamRect = stream.getBoundingClientRect();
      const messageRect = message.getBoundingClientRect();
      stream.scrollTop += messageRect.top - streamRect.top - Number(anchor.topOffset || 0);
      return false;
    }
  }
  const maxScrollTop = Math.max(0, stream.scrollHeight - stream.clientHeight);
  stream.scrollTop = Math.min(Number(anchor.scrollTop || 0), maxScrollTop);
  return false;
}


/**
 * Track pending attachment descriptors uploaded for the current composer.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @returns {Object[]} Mutable list of descriptors.
 */
function pendingAttachments(root) {
  if (!Array.isArray(root.__cwChatTerminalPendingAttachments)) {
    root.__cwChatTerminalPendingAttachments = [];
  }
  return root.__cwChatTerminalPendingAttachments;
}

/**
 * Return the canonical graph-instance scope from the editor URL.
 *
 * @returns {{workspaceProjectId:string, graphId:string, instanceId:string}} Scope identifiers.
 */
function graphScopeFromLocation() {
  const parts = window.location.pathname.split("/").filter(Boolean).map((part) => decodeURIComponent(part));
  if (parts[0] === "project" && parts[2] && parts[3]) {
    const marker = parts[2];
    const instanceMarker = parts[4];
    return {
      workspaceProjectId: parts[1] || "1",
      graphId: marker === "blueprint" ? parts[3] : "",
      instanceId: instanceMarker === "instance" ? (parts[5] || "1") : "1",
    };
  }
  return { workspaceProjectId: "1", graphId: "", instanceId: "1" };
}

/**
 * Return the upload endpoint for the currently opened graph instance.
 *
 * @returns {string} Upload URL.
 */
function attachmentUploadUrl() {
  const scope = graphScopeFromLocation();
  if (!scope.graphId) {
    throw new Error("graph_scope_missing");
  }
  return `/api/projects/${encodeURIComponent(scope.workspaceProjectId)}/graphs/${encodeURIComponent(scope.graphId)}/instances/${encodeURIComponent(scope.instanceId || "1")}/attachments`;
}

/**
 * Format a file size for compact attachment chips.
 *
 * @param {number} size - Size in bytes.
 * @returns {string} Display size.
 */
function formatAttachmentSize(size) {
  const value = Number(size || 0);
  if (!Number.isFinite(value) || value <= 0) {
    return "";
  }
  if (value < 1024) {
    return `${value} o`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} Ko`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} Mo`;
}

/**
 * Render pending attachment chips below the composer.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 */
function renderPendingAttachments(root) {
  const mount = root.querySelector("[data-chat-terminal-pending-attachments]");
  if (!mount) {
    return;
  }
  mount.replaceChildren();
  pendingAttachments(root).forEach((attachment, index) => {
    const chip = document.createElement("span");
    chip.className = "chat-terminal-pending-attachment";
    const name = document.createElement("span");
    name.textContent = attachment.name || "attachment";
    chip.append(name);
    const size = formatAttachmentSize(attachment.size);
    if (size) {
      const meta = document.createElement("small");
      meta.textContent = size;
      chip.append(meta);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "Retirer cette pièce jointe";
    remove.addEventListener("click", () => {
      pendingAttachments(root).splice(index, 1);
      renderPendingAttachments(root);
    });
    chip.append(remove);
    mount.append(chip);
  });
}

/**
 * Upload one file to the graph-instance attachment endpoint.
 *
 * @param {File} file - Browser file object.
 * @returns {Promise<Object>} Attachment descriptor returned by the server.
 */
async function uploadAttachment(file) {
  const form = new FormData();
  form.append("file", file, file.name || "attachment");
  const response = await fetch(attachmentUploadUrl(), { method: "POST", body: form });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload?.ok !== true || !payload.attachment) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload.attachment;
}

/**
 * Upload a FileList/array and append descriptors to the composer.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {Object} api - Block UI API.
 * @param {File[]} files - Files to upload.
 */
async function addAttachments(root, api, files) {
  const candidates = Array.from(files || []).filter((file) => file && Number(file.size || 0) > 0);
  if (!candidates.length) {
    return;
  }
  const dropzone = root.querySelector("[data-chat-terminal-dropzone]");
  dropzone?.classList.add("is-uploading");
  try {
    for (const file of candidates) {
      pendingAttachments(root).push(await uploadAttachment(file));
    }
    renderPendingAttachments(root);
  } catch (error) {
    api.log?.(`[chat-terminal-attachment-error] ${error.message}`);
  } finally {
    dropzone?.classList.remove("is-uploading");
  }
}

/**
 * Render attachment chips on a message appended optimistically in the browser.
 *
 * @param {HTMLElement} article - Message article.
 * @param {Object[]} attachments - Attachment descriptors.
 */
function appendAttachmentChips(article, attachments) {
  const items = Array.isArray(attachments) ? attachments : [];
  if (!items.length) {
    return;
  }
  const mount = document.createElement("div");
  mount.className = "chat-terminal-attachments";
  items.forEach((attachment) => {
    const href = String(attachment.url || "");
    const chip = href ? document.createElement("a") : document.createElement("span");
    chip.className = "chat-terminal-attachment";
    chip.textContent = attachment.name || "attachment";
    if (href) {
      chip.href = href;
      chip.target = "_blank";
      chip.rel = "noopener";
    }
    const size = formatAttachmentSize(attachment.size);
    if (size) {
      const meta = document.createElement("small");
      meta.textContent = size;
      chip.append(meta);
    }
    mount.append(chip);
  });
  article.append(mount);
}

/**
 * Build a block-owned outgoing message entry without requiring a graph patch.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {string} message - User-authored message.
 * @param {Object[]} attachments - Uploaded attachment descriptors.
 * @returns {Object} Normalized outgoing message entry.
 */
function createOutgoingEntry(root, message, attachments = []) {
  const originId = root.dataset.runtimeNodeId || root.dataset.nodeId || "chat_terminal";
  const originName = root.dataset.nodeTitle || "Chat terminal";
  const normalizedAttachments = Array.isArray(attachments) ? attachments : [];
  const rawPayload = { origin_id: originId, origin_name: originName, msg: String(message || "") };
  if (normalizedAttachments.length) {
    rawPayload.attachments = normalizedAttachments;
  }
  return {
    message_id: `out-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
    direction: "out",
    origin_id: originId,
    origin_name: originName,
    msg: String(message || ""),
    attachments: normalizedAttachments,
    raw: JSON.stringify(rawPayload),
    created_at: Date.now() / 1000,
  };
}

/**
 * Keep the visible stream bounded to the same history limit as the backend.
 *
 * @param {HTMLElement} stream - Message stream container.
 */
function trimVisibleMessages(stream) {
  while (stream.querySelectorAll("[data-chat-terminal-message]").length > 20) {
    stream.querySelector("[data-chat-terminal-message]")?.remove();
  }
}

/**
 * Append a backend-rendered message fragment when the block supplied one.
 *
 * @param {HTMLElement} stream - Message stream container.
 * @param {string} html - Server-rendered message article.
 * @returns {boolean} True when the fragment was appended.
 */
function appendRenderedMessage(stream, html) {
  const renderedHtml = String(html || "").trim();
  if (!renderedHtml) {
    return false;
  }
  const template = document.createElement("template");
  template.innerHTML = renderedHtml;
  const article = template.content.querySelector("[data-chat-terminal-message]");
  if (!article) {
    return false;
  }
  stream.append(article);
  return true;
}

/**
 * Append one local chat message to the visible terminal stream.
 *
 * @param {HTMLElement} stream - Message stream container.
 * @param {Object} entry - Normalized chat message returned by the block action.
 * @param {Object} [options] - Rendering options.
 * @param {boolean} [options.stickToBottom=true] - Scroll down after append.
 */
function appendMessage(stream, entry, { stickToBottom = true } = {}) {
  if (!stream || !entry) {
    return;
  }
  const idValue = messageId(entry);
  if (idValue && stream.querySelector(`[data-chat-terminal-message-id="${cssEscape(idValue)}"]`)) {
    return;
  }
  const empty = stream.querySelector("[data-chat-terminal-empty]");
  if (empty) {
    empty.remove();
  }
  if (appendRenderedMessage(stream, entry.__renderedHtml || entry.message_html)) {
    trimVisibleMessages(stream);
    if (stickToBottom) {
      scrollStreamToBottom(stream);
    }
    return;
  }
  const article = document.createElement("article");
  article.className = `chat-terminal-message is-${entry.direction === "out" ? "out" : "in"}`;
  article.dataset.chatTerminalMessage = "true";
  if (idValue) {
    article.dataset.chatTerminalMessageId = idValue;
  }

  const header = document.createElement("header");
  const origin = document.createElement("span");
  origin.className = "chat-terminal-origin";
  origin.textContent = entry.origin_name || entry.origin_id || "unkbloc";
  const meta = document.createElement("details");
  meta.className = "chat-terminal-message-meta";
  const summary = document.createElement("summary");
  summary.textContent = entry.direction === "out" ? "sortant" : "entrant";
  const id = document.createElement("span");
  id.className = "chat-terminal-id";
  id.textContent = idValue;
  meta.append(summary, id);
  header.append(origin, meta);

  const body = document.createElement("div");
  body.className = "chat-terminal-text chat-terminal-markdown";
  body.textContent = entry.msg || "";
  article.append(header, body);
  appendAttachmentChips(article, entry.attachments || []);
  stream.append(article);
  trimVisibleMessages(stream);
  if (stickToBottom) {
    scrollStreamToBottom(stream);
  }
}

/**
 * Activate one modal tab and hide the other panels.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {string} tabName - Target tab identifier.
 */
function activateTab(root, tabName) {
  const tabs = Array.from(root.querySelectorAll("[data-chat-terminal-tab]"));
  const panels = Array.from(root.querySelectorAll("[data-chat-terminal-panel]"));
  tabs.forEach((tab) => {
    const active = tab.dataset.chatTerminalTab === tabName;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
    tab.tabIndex = active ? 0 : -1;
  });
  panels.forEach((panel) => {
    const active = panel.dataset.chatTerminalPanel === tabName;
    panel.classList.toggle("is-active", active);
    panel.hidden = !active;
  });
  if (tabName === "terminal") {
    const stream = root.querySelector("[data-chat-terminal-stream]");
    if (stream) {
      scrollStreamToBottom(stream);
    }
  }
}

/**
 * Wire the block-owned modal tab bar without relying on framework-specific tabs.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 */
function mountTabs(root) {
  const tabs = Array.from(root.querySelectorAll("[data-chat-terminal-tab]"));
  if (!tabs.length) {
    return;
  }
  tabs.forEach((tab, index) => {
    tab.tabIndex = tab.classList.contains("is-active") ? 0 : -1;
    tab.addEventListener("click", () => activateTab(root, tab.dataset.chatTerminalTab || "terminal"));
    tab.addEventListener("keydown", (event) => {
      const lastIndex = tabs.length - 1;
      let nextIndex = index;
      if (event.key === "ArrowRight") {
        nextIndex = index === lastIndex ? 0 : index + 1;
      } else if (event.key === "ArrowLeft") {
        nextIndex = index === 0 ? lastIndex : index - 1;
      } else if (event.key === "Home") {
        nextIndex = 0;
      } else if (event.key === "End") {
        nextIndex = lastIndex;
      } else {
        return;
      }
      event.preventDefault();
      tabs[nextIndex].focus();
      activateTab(root, tabs[nextIndex].dataset.chatTerminalTab || "terminal");
    });
  });
}

/**
 * Replace the visible stream from a server-rendered message fragment.
 *
 * @param {HTMLElement} stream - Message stream container.
 * @param {string} html - Block-rendered message HTML fragment.
 * @returns {boolean} True when the stream should remain stuck to the bottom.
 */
function replaceStreamHtml(stream, html) {
  if (!stream || typeof html !== "string") {
    return false;
  }
  const anchor = captureScrollAnchor(stream);
  if (stream.dataset.chatTerminalHistoryHtml === html) {
    return anchor.mode === "bottom";
  }
  stream.innerHTML = html;
  stream.dataset.chatTerminalHistoryHtml = html;
  return restoreScrollAnchor(stream, anchor);
}

/**
 * Refresh recent messages through the block-owned history endpoint.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {Object} api - Block UI API exposing blockRequest.
 */
async function refreshHistory(root, api) {
  const stream = root.querySelector("[data-chat-terminal-stream]");
  if (!stream || root.dataset.chatTerminalHistoryBusy === "true") {
    return;
  }
  root.dataset.chatTerminalHistoryBusy = "true";
  try {
    const result = await api.blockRequest("history", {
      method: "POST",
      payload: {
        values: {
          limit: 20,
          runtime_node_id: root.dataset.runtimeNodeId || root.dataset.nodeId || "",
        },
      },
    });
    if (result?.error) {
      throw new Error(result.error);
    }
    const shouldStickToBottom = replaceStreamHtml(stream, result?.messages_html || "");
    for (const entry of localMessages(root)) {
      appendMessage(stream, entry, { stickToBottom: shouldStickToBottom });
    }
    if (shouldStickToBottom) {
      scrollStreamToBottom(stream);
    }
  } catch (error) {
    api.log?.(`[chat-terminal-refresh-error] ${error.message}`);
  } finally {
    root.dataset.chatTerminalHistoryBusy = "false";
  }
}

/**
 * Publish a freshly sent message to the active runtime when it is loaded.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {Object} api - Block UI API exposing runtime actions.
 * @param {Object} result - Block action result containing the serialized output.
 * @returns {Promise<void>} Resolves when no runtime is loaded or publication finished.
 */
async function publishSentMessage(root, api, result) {
  const runtime = api.actions?.getActiveRuntimeContext?.() || {};
  if (!runtime.active || !api.actions?.publishActiveOutput || !result?.output_value) {
    return;
  }
  await api.actions.publishActiveOutput({
    node_id: root.dataset.nodeId || "",
    port_name: result.port_name || "msg",
    value: result.output_value,
    content_type: result.content_type || "application/json",
  });
}

/**
 * Persist one message into the block-owned memory file.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {Object} api - Block UI API exposing blockRequest.
 * @param {Object} entry - Normalized chat message entry.
 * @returns {Promise<Object|null>} Remembered entry when persistence succeeded.
 */
async function rememberMessage(root, api, entry) {
  if (!api.blockRequest || !entry) {
    return null;
  }
  const result = await api.blockRequest("remember", {
    method: "POST",
    payload: {
      values: {
        runtime_node_id: root.dataset.runtimeNodeId || root.dataset.nodeId || "",
        entry,
        limit: 20,
      },
    },
  });
  if (result?.error) {
    throw new Error(result.error);
  }
  return result || null;
}

/**
 * Send directly to a loaded active runtime without mutating graph config first.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {Object} api - Block UI API exposing runtime actions.
 * @param {string} message - User-authored message.
 * @param {Object[]} attachments - Uploaded attachment descriptors.
 * @returns {Promise<Object>} Sent local entry.
 */
async function sendActiveMessage(root, api, message, attachments = []) {
  const entry = createOutgoingEntry(root, message, attachments);
  await publishSentMessage(root, api, {
    output_value: entry.raw,
    content_type: "application/json",
    port_name: "msg",
  });
  const rememberResult = await rememberMessage(root, api, entry);
  if (rememberResult?.message_html) {
    entry.__renderedHtml = rememberResult.message_html;
  }
  localMessages(root).push(entry);
  while (localMessages(root).length > 20) {
    localMessages(root).shift();
  }
  return entry;
}

/**
 * Stop the autonomous history refresh owned by one modal instance.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 */
function stopHistoryRefresh(root) {
  const timer = Number(root?.dataset?.chatTerminalHistoryTimer || 0);
  if (timer) {
    window.clearInterval(timer);
  }
  if (root?.__cwChatTerminalPageHideHandler) {
    window.removeEventListener("pagehide", root.__cwChatTerminalPageHideHandler);
    delete root.__cwChatTerminalPageHideHandler;
  }
  if (root?.dataset) {
    delete root.dataset.chatTerminalHistoryTimer;
    delete root.dataset.chatTerminalHistoryBusy;
  }
}

/**
 * Start autonomous message refresh for the modal lifetime.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {Object} api - Block UI API exposing blockRequest.
 */
function mountHistoryRefresh(root, api) {
  if (root.dataset.chatTerminalHistoryTimer) {
    void refreshHistory(root, api);
    return;
  }
  const refresh = () => {
    if (!root.isConnected) {
      stopHistoryRefresh(root);
      return;
    }
    if (document.visibilityState === "hidden") {
      return;
    }
    void refreshHistory(root, api);
  };
  root.__cwChatTerminalPageHideHandler = () => stopHistoryRefresh(root);
  window.addEventListener("pagehide", root.__cwChatTerminalPageHideHandler);
  void refreshHistory(root, api);
  const timer = window.setInterval(refresh, 1200);
  root.dataset.chatTerminalHistoryTimer = String(timer);
}


/**
 * Return the best browser-supported audio MIME type for short chat dictation.
 *
 * @returns {string} MIME type accepted by MediaRecorder, or empty for default.
 */
function preferredAudioMimeType() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg",
    "audio/mp4",
  ];
  if (!window.MediaRecorder?.isTypeSupported) {
    return "";
  }
  return candidates.find((mimeType) => window.MediaRecorder.isTypeSupported(mimeType)) || "";
}

/**
 * Convert a recorded microphone blob to a base64 data URL for block.py.
 *
 * @param {Blob} blob - Temporary browser microphone capture.
 * @returns {Promise<string>} Data URL containing the audio bytes.
 */
function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result || "")));
    reader.addEventListener("error", () => reject(reader.error || new Error("audio_read_failed")));
    reader.readAsDataURL(blob);
  });
}

/**
 * Stop all tracks from a microphone stream after a temporary capture.
 *
 * @param {MediaStream|null} stream - Active microphone stream.
 */
function stopStream(stream) {
  for (const track of Array.from(stream?.getTracks?.() || [])) {
    track.stop();
  }
}

/**
 * Update the discreet speech-to-text status below the composer.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {string} message - Human-readable status.
 * @param {boolean} isError - Whether the message is an error state.
 */
function setSpeechStatus(root, message, isError = false) {
  const status = root.querySelector("[data-chat-terminal-speech-status]");
  if (!status) {
    return;
  }
  status.textContent = message || "";
  status.classList.toggle("is-error", Boolean(isError));
}

/**
 * Insert a transcript at the current cursor position in the chat composer.
 *
 * @param {HTMLTextAreaElement} input - Chat composer textarea.
 * @param {string} transcript - Text returned by the STT action.
 */
function insertTranscript(input, transcript) {
  const text = String(transcript || "").trim();
  if (!text) {
    return;
  }
  const start = Number.isFinite(input.selectionStart) ? input.selectionStart : input.value.length;
  const end = Number.isFinite(input.selectionEnd) ? input.selectionEnd : start;
  const before = input.value.slice(0, start);
  const after = input.value.slice(end);
  const separator = before && !/\s$/.test(before) ? " " : "";
  input.value = `${before}${separator}${text}${after}`;
  const cursor = before.length + separator.length + text.length;
  input.setSelectionRange(cursor, cursor);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

/**
 * Wire press-and-hold microphone capture to the block-owned STT action.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {Object} api - Block UI API used to call block-owned actions.
 * @param {HTMLTextAreaElement} input - Chat composer textarea.
 */
function mountSpeechCapture(root, api, input) {
  const mic = root.querySelector("[data-chat-terminal-mic]");
  if (!mic) {
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    mic.disabled = true;
    setSpeechStatus(root, "Micro navigateur indisponible sur cette page.", true);
    return;
  }
  let recorder = null;
  let stream = null;
  let chunks = [];
  let startedAt = 0;
  let recordingRequested = false;

  const stopRecording = () => {
    recordingRequested = false;
    if (!recorder || recorder.state === "inactive") {
      return;
    }
    recorder.stop();
  };

  const startRecording = async (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (recorder && recorder.state !== "inactive") {
      return;
    }
    recordingRequested = true;
    chunks = [];
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      const mimeType = preferredAudioMimeType();
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      recorder.addEventListener("dataavailable", (chunkEvent) => {
        if (chunkEvent.data?.size) {
          chunks.push(chunkEvent.data);
        }
      });
      recorder.addEventListener("stop", async () => {
        mic.classList.remove("is-recording");
        mic.disabled = true;
        stopStream(stream);
        stream = null;
        const blob = new Blob(chunks, { type: recorder?.mimeType || mimeType || "audio/webm" });
        chunks = [];
        if (!blob.size) {
          mic.disabled = false;
          setSpeechStatus(root, "Aucun audio capture.", true);
          return;
        }
        setSpeechStatus(root, "Transcription en cours...");
        try {
          const dataUrl = await blobToDataUrl(blob);
          const result = await api.applyAction("chat_transcribe_audio", {
            data_url: dataUrl,
            mime_type: blob.type || "audio/webm",
            duration_ms: Date.now() - startedAt,
          });
          if (result?.error) {
            throw new Error(result.error);
          }
          insertTranscript(input, result?.transcript || "");
          setSpeechStatus(root, "Transcription ajoutee au message.");
          input.focus();
        } catch (error) {
          setSpeechStatus(root, `Transcription impossible: ${error.message}`, true);
          api.log?.(`[chat-terminal-stt-error] ${error.message}`);
        } finally {
          mic.disabled = false;
        }
      });
      startedAt = Date.now();
      recorder.start();
      mic.classList.add("is-recording");
      setSpeechStatus(root, "Enregistrement en cours, relachez pour transcrire.");
      if (!recordingRequested) {
        stopRecording();
      }
    } catch (error) {
      recordingRequested = false;
      stopStream(stream);
      stream = null;
      mic.classList.remove("is-recording");
      setSpeechStatus(root, `Micro indisponible: ${error.message}`, true);
    }
  };

  mic.addEventListener("pointerdown", (event) => {
    mic.setPointerCapture?.(event.pointerId);
    void startRecording(event);
  });
  mic.addEventListener("pointerup", (event) => {
    event.preventDefault();
    event.stopPropagation();
    stopRecording();
  });
  mic.addEventListener("pointercancel", stopRecording);
  mic.addEventListener("lostpointercapture", stopRecording);
  mic.addEventListener("keydown", (event) => {
    if (event.key !== " " && event.key !== "Enter") {
      return;
    }
    void startRecording(event);
  });
  mic.addEventListener("keyup", (event) => {
    if (event.key === " " || event.key === "Enter") {
      event.preventDefault();
      stopRecording();
    }
  });
}


/**
 * Resize the composer so short messages keep the terminal focused on history.
 *
 * @param {HTMLTextAreaElement} input - Message textarea.
 */
function resizeComposerInput(input) {
  if (!(input instanceof HTMLTextAreaElement)) {
    return;
  }
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 156)}px`;
}

/**
 * Keep the message composer compact until the user writes longer text.
 *
 * @param {HTMLTextAreaElement} input - Message textarea.
 */
function mountComposerResize(input) {
  if (!(input instanceof HTMLTextAreaElement)) {
    return;
  }
  resizeComposerInput(input);
  input.addEventListener("input", () => resizeComposerInput(input));
}

/**
 * Wire paste, drop and file-picker attachment uploads for the composer.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {Object} api - Block UI API.
 * @param {HTMLTextAreaElement} input - Message textarea.
 */
function mountAttachments(root, api, input) {
  const attach = root.querySelector("[data-chat-terminal-attach]");
  const fileInput = root.querySelector("[data-chat-terminal-file-input]");
  const dropzone = root.querySelector("[data-chat-terminal-dropzone]");
  attach?.addEventListener("click", () => fileInput?.click());
  fileInput?.addEventListener("change", () => {
    void addAttachments(root, api, Array.from(fileInput.files || [])).finally(() => {
      fileInput.value = "";
    });
  });
  input.addEventListener("paste", (event) => {
    const files = Array.from(event.clipboardData?.files || []);
    if (!files.length) {
      return;
    }
    event.preventDefault();
    void addAttachments(root, api, files);
  });
  const cancelDrag = (event) => {
    event.preventDefault();
    event.stopPropagation();
  };
  [dropzone, input].forEach((target) => {
    target?.addEventListener("dragover", (event) => {
      cancelDrag(event);
      dropzone?.classList.add("is-drag-over");
    });
    target?.addEventListener("dragleave", () => dropzone?.classList.remove("is-drag-over"));
    target?.addEventListener("drop", (event) => {
      cancelDrag(event);
      dropzone?.classList.remove("is-drag-over");
      void addAttachments(root, api, Array.from(event.dataTransfer?.files || []));
    });
  });
}

/**
 * Mount chat terminal modal behavior: tabs, message send action and shortcuts.
 *
 * @param {HTMLElement} root - Chat terminal modal root.
 * @param {Object} api - Block UI API used to call block-owned actions.
 */
export function mount(root, api) {
  mountTabs(root);
  mountHistoryRefresh(root, api);
  const form = root.querySelector("[data-chat-terminal-form]");
  const input = root.querySelector("[data-chat-terminal-input]");
  const stream = root.querySelector("[data-chat-terminal-stream]");
  if (!form || !input || !stream) {
    return;
  }
  mountSpeechCapture(root, api, input);
  mountAttachments(root, api, input);
  mountComposerResize(input);
  renderPendingAttachments(root);
  scrollStreamToBottom(stream);

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const message = String(input.value || "").trim();
    const attachments = pendingAttachments(root).slice();
    if (!message && !attachments.length) {
      return;
    }
    const submit = form.querySelector("[data-chat-terminal-send]");
    if (submit) {
      submit.disabled = true;
    }
    const runtime = api.actions?.getActiveRuntimeContext?.() || {};
    const sendPromise = runtime.active && api.actions?.publishActiveOutput
      ? sendActiveMessage(root, api, message, attachments)
      : api.applyAction("chat_send_message", { message, attachments }).then(async (result) => {
        await publishSentMessage(root, api, result);
        if (result?.message_entry && result?.message_html) {
          result.message_entry.__renderedHtml = result.message_html;
        }
        return result.message_entry;
      });

    void sendPromise.then((entry) => {
      appendMessage(stream, entry);
      input.value = "";
      resizeComposerInput(input);
      pendingAttachments(root).splice(0);
      renderPendingAttachments(root);
    }).catch((error) => {
      api.log?.(`[chat-terminal-error] ${error.message}`);
    }).finally(() => {
      if (submit) {
        submit.disabled = false;
      }
      input.focus();
    });
  });

  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    event.preventDefault();
    form.requestSubmit();
  });
}
