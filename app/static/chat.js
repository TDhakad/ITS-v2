const chatState = {
  conversationId: window.crypto?.randomUUID?.() || `session-${Date.now()}`,
  isSending: false,
};

const messageList = document.querySelector("#message-list");
const chatForm = document.querySelector("#chat-form");
const chatInput = document.querySelector("#chat-input");
const sendButton = document.querySelector("#send-button");
const chatStatus = document.querySelector("#chat-status");
const ticketTitle = document.querySelector("#ticket-title");
const ticketId = document.querySelector("#ticket-id");
const ticketPriority = document.querySelector("#ticket-priority");
const ticketCategory = document.querySelector("#ticket-category");
const citationList = document.querySelector("#citation-list");

function setStatus(text, mode = "idle") {
  chatStatus.textContent = text;
  chatStatus.classList.toggle("is-active", mode === "active");
  chatStatus.classList.toggle("is-error", mode === "error");
}

function normalizeText(value, fallback = "") {
  if (typeof value === "string") {
    return value.trim() || fallback;
  }
  if (value === null || value === undefined) {
    return fallback;
  }
  return String(value);
}

function pick(obj, keys, fallback = "") {
  if (!obj || typeof obj !== "object") {
    return fallback;
  }
  for (const key of keys) {
    if (obj[key] !== undefined && obj[key] !== null && obj[key] !== "") {
      return obj[key];
    }
  }
  return fallback;
}

function appendMessage(role, text) {
  const article = document.createElement("article");
  const isUser = role === "user";
  article.className = `message ${isUser ? "message-user" : "message-assistant"}`;

  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = isUser ? "U" : "A";

  const body = document.createElement("div");
  body.className = "message-body";

  const meta = document.createElement("p");
  meta.className = "message-meta";
  meta.textContent = isUser ? "You" : "Assistant";

  const content = document.createElement("div");
  content.className = "message-content";
  const rawText = normalizeText(text, "No response content returned.");
  if (!isUser && typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
    content.innerHTML = DOMPurify.sanitize(marked.parse(rawText));
  } else {
    content.textContent = rawText;
  }

  body.append(meta, content);
  article.append(avatar, body);
  messageList.append(article);
  messageList.scrollTop = messageList.scrollHeight;
}

function normalizeCitations(response) {
  const citations = pick(response, ["citations", "sources", "documents", "references"], []);
  return Array.isArray(citations) ? citations : [];
}

function renderCitations(citations) {
  citationList.replaceChildren();
  if (!citations.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No knowledge references returned.";
    citationList.append(empty);
    return;
  }

  for (const citation of citations.slice(0, 5)) {
    const item = document.createElement("article");
    item.className = "citation";

    const title = document.createElement("p");
    title.className = "citation-title";
    title.textContent = normalizeText(
      pick(citation, ["title", "source", "document", "name"], "Knowledge source"),
    );

    const text = document.createElement("p");
    text.className = "citation-text";
    text.textContent = normalizeText(
      pick(citation, ["snippet", "content", "text", "summary"], "Reference matched this issue."),
    );

    item.append(title, text);
    citationList.append(item);
  }
}

function renderTicket(ticket) {
  if (!ticket || typeof ticket !== "object") {
    return;
  }

  const id = pick(ticket, ["id", "ticket_id", "number"], "Pending");
  ticketTitle.textContent = normalizeText(pick(ticket, ["title", "summary"], "Ticket drafted"));
  ticketId.textContent = normalizeText(id, "Pending");
  ticketPriority.textContent = normalizeText(pick(ticket, ["priority", "severity"], "Unassigned"));
  ticketCategory.textContent = normalizeText(pick(ticket, ["category", "type"], "Unclassified"));
}

function normalizeChatResponse(data) {
  const responseText = pick(data, ["message", "answer", "response", "content", "assistant_message"], "");
  const nestedTicket = pick(data, ["ticket", "created_ticket", "updated_ticket"], null);
  return {
    text: normalizeText(responseText, "I received the message, but the API returned no answer text."),
    ticket: nestedTicket && typeof nestedTicket === "object" ? nestedTicket : data?.ticket_id ? data : null,
    citations: normalizeCitations(data),
  };
}

async function sendMessage(message) {
  chatState.isSending = true;
  sendButton.disabled = true;
  chatInput.disabled = true;
  setStatus("Sending", "active");

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        conversation_id: chatState.conversationId,
      }),
    });

    if (!response.ok) {
      throw new Error(`Chat request failed with ${response.status}`);
    }

    const data = await response.json();
    chatState.conversationId = pick(data, ["conversation_id", "session_id"], chatState.conversationId);
    const normalized = normalizeChatResponse(data);
    appendMessage("assistant", normalized.text);
    renderTicket(normalized.ticket);
    renderCitations(normalized.citations);
    setStatus("Ready");
  } catch (error) {
    appendMessage("assistant", "I could not reach the chat service. Please try again.");
    setStatus("Connection error", "error");
    console.error(error);
  } finally {
    chatState.isSending = false;
    sendButton.disabled = false;
    chatInput.disabled = false;
    chatInput.focus();
  }
}

chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (chatState.isSending) {
    return;
  }

  const message = chatInput.value.trim();
  if (!message) {
    return;
  }

  appendMessage("user", message);
  chatInput.value = "";
  sendMessage(message);
});

chatInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    chatForm.requestSubmit();
  }
});
