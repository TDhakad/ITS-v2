import { Bot, FileText, MessageSquare, Plus, Send, Shield, Ticket as TicketIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { api } from "../api/client";
import { formatTime, generateId, initials, kbReferenceHref } from "../lib";
import type { ApiUser, ChatHistoryMessage, ChatMessage, ChatThread, LoadState, Ticket } from "../types";
import { Button, EmptyState, LoadingState } from "./common";
import { MarkdownContent } from "./MarkdownContent";

interface AssistantPageProps {
  tickets: Ticket[];
  user: ApiUser | null;
  projectId: number | null;
  onTicketCreated: (ticket: Ticket) => void;
  onTicketSelect: (ticketId: number) => void;
}

const suggestions = [
  "Which tickets are at SLA risk?",
  "Summarize open access requests",
  "Draft a reply for the newest infrastructure ticket",
  "Show onboarding gaps"
];

const defaultAssistantText =
  "I have context from the ticket API and knowledge references returned by the assistant endpoint.";

export function AssistantPage({
  tickets,
  user,
  projectId,
  onTicketCreated,
  onTicketSelect
}: AssistantPageProps) {
  const [conversationId, setConversationId] = useState(() => defaultThreadId(null));
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>(() => [defaultAssistantMessage()]);
  const [state, setState] = useState<LoadState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [threads, setThreads] = useState<ChatThread[]>([]);

  const recentTickets = useMemo(() => tickets.slice(0, 6), [tickets]);

  // Fetch thread list once on mount and after each message so the sidebar stays fresh.
  const refreshThreads = useCallback(() => {
    api.getChatThreads().then((res) => setThreads(res.threads)).catch(() => {});
  }, []);

  useEffect(() => {
    refreshThreads();
  }, [refreshThreads]);

  useEffect(() => {
    let active = true;
    const storageKey = threadStorageKey(user);
    const requestedThreadId = readThreadId(storageKey) ?? defaultThreadId(user);

    setState("loading");
    setError(null);

    api
      .getChatHistory(requestedThreadId)
      .then((history) => {
        if (!active) {
          return;
        }
        const nextThreadId = history.thread_id || requestedThreadId;
        const restoredMessages = history.messages.map(historyMessageToChatMessage);
        setConversationId(nextThreadId);
        rememberThreadId(storageKey, nextThreadId);
        setMessages(restoredMessages.length ? restoredMessages : [defaultAssistantMessage()]);
        setState("idle");
      })
      .catch(() => {
        if (!active) {
          return;
        }
        setConversationId(requestedThreadId);
        setMessages([defaultAssistantMessage()]);
        setState("idle");
      });

    return () => {
      active = false;
    };
  }, [user?.id]);

  function loadThread(threadId: string) {
    setState("loading");
    setError(null);
    api
      .getChatHistory(threadId)
      .then((history) => {
        const restoredMessages = history.messages.map(historyMessageToChatMessage);
        setConversationId(threadId);
        rememberThreadId(threadStorageKey(user), threadId);
        setMessages(restoredMessages.length ? restoredMessages : [defaultAssistantMessage()]);
        setState("idle");
      })
      .catch(() => {
        setConversationId(threadId);
        setMessages([defaultAssistantMessage()]);
        setState("idle");
      });
  }

  async function submitMessage(value: string) {
    const trimmed = value.trim();
    if (!trimmed || state === "loading") {
      return;
    }

    const userMessage: ChatMessage = {
      id: generateId("msg"),
      role: "user",
      content: trimmed,
      createdAt: new Date().toISOString()
    };

    setMessages((current) => [...current, userMessage]);
    setInput("");
    setState("loading");
    setError(null);

    try {
      const response = await api.chat({
        message: trimmed,
        conversation_id: conversationId,
        thread_id: conversationId,
        user_id: user ? String(user.id) : "anonymous",
        environment: "unknown",
        clearance: user?.clearance ?? "public",
        project_id: projectId
      });

      if (response.ticket) {
        onTicketCreated(response.ticket);
      }

      setConversationId(response.thread_id || response.conversation_id || conversationId);
      rememberThreadId(threadStorageKey(user), response.thread_id || response.conversation_id || conversationId);
      setMessages((current) => [
        ...current,
        {
          id: generateId("msg"),
          role: "assistant",
          content: response.response || response.message || "No response content returned.",
          createdAt: new Date().toISOString(),
          citations: response.citations ?? response.references,
          ticket: response.ticket
        }
      ]);
      setState("ready");
      refreshThreads();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "Assistant request failed.";
      setError(message);
      setMessages((current) => [
        ...current,
        {
          id: generateId("msg"),
          role: "assistant",
          content: message,
          createdAt: new Date().toISOString()
        }
      ]);
      setState("error");
    }
  }

  return (
    <section className="assistant-page" aria-label="AI assistant">
      <aside className="chat-history">
        <div className="chat-history-top">
          <strong>Conversations</strong>
          <Button
            icon={<Plus size={15} aria-hidden="true" />}
            onClick={() => {
              const nextThreadId = newThreadId(user);
              setConversationId(nextThreadId);
              rememberThreadId(threadStorageKey(user), nextThreadId);
              setMessages([defaultAssistantMessage("New conversation started.")]);
              setError(null);
              setState("idle");
            }}
          >
            New chat
          </Button>
        </div>
        <div className="chat-history-list">
          {threads.length ? (
            threads.map((thread) => (
              <button
                className={`history-item${thread.thread_id === conversationId ? " active" : ""}`}
                key={thread.thread_id}
                onClick={() => loadThread(thread.thread_id)}
              >
                <span className="history-item-preview">
                  <MessageSquare size={13} aria-hidden="true" />
                  {thread.preview || "Conversation"}
                </span>
                <small>{thread.last_at ? formatTime(thread.last_at) : ""}</small>
              </button>
            ))
          ) : (
            <p className="muted-text padded">No past conversations.</p>
          )}
          {recentTickets.length ? (
            <>
              <p className="side-section">Recent tickets</p>
              {recentTickets.map((ticket) => (
                <button className="history-item" key={ticket.id} onClick={() => onTicketSelect(ticket.id)}>
                  <span>{ticket.title}</span>
                  <small>#{ticket.id} / {formatTime(ticket.updated_at)}</small>
                </button>
              ))}
            </>
          ) : null}
        </div>
      </aside>
      <div className="chat-main">
        <header className="chat-topbar">
          <strong>AI Assistant</strong>
          <span className="context-pill">
            <TicketIcon size={14} aria-hidden="true" />
            {tickets.length} tickets in context
          </span>
        </header>
        <div className="message-list" aria-live="polite">
          {messages.map((message) => (
            <article className={`bubble-row ${message.role === "user" ? "mine" : ""}`} key={message.id}>
              <span className={`bubble-avatar ${message.role === "user" ? "mine" : "ai"}`}>
                {message.role === "user" ? initials(user?.display_name, "U") : "AI"}
              </span>
              <div className={`bubble ${message.role === "user" ? "mine" : "ai"}`}>
                <MarkdownContent
                  content={message.content}
                  onTicketSelect={onTicketSelect}
                />
                {message.citations?.length ? (
                  <div className="source-row">
                    {message.citations.slice(0, 4).map((citation) => {
                      const label = citation.title ?? citation.source ?? "Reference";
                      const href = kbReferenceHref(citation);
                      return href ? (
                        <a
                          className="source-chip kb-link"
                          href={href}
                          target="_blank"
                          rel="noreferrer"
                          key={citation.kb_id ?? citation.title ?? citation.source}
                        >
                          {label}
                        </a>
                      ) : (
                        <span className="source-chip" key={citation.kb_id ?? citation.title ?? citation.source}>
                          {label}
                        </span>
                      );
                    })}
                  </div>
                ) : null}
                {message.ticket ? (
                  <button className="created-ticket-link" onClick={() => onTicketSelect(message.ticket!.id)}>
                    Created ticket #{message.ticket.id}
                  </button>
                ) : null}
                <time>{formatTime(message.createdAt)}</time>
              </div>
            </article>
          ))}
          {state === "loading" ? <LoadingState label="Assistant is responding" /> : null}
          {error && state === "error" ? <EmptyState title="Assistant error">{error}</EmptyState> : null}
        </div>
        <div className="suggestion-row">
          {suggestions.map((suggestion) => (
            <button key={suggestion} onClick={() => submitMessage(suggestion)}>
              {suggestion}
            </button>
          ))}
        </div>
        <form
          className="chat-composer"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            void submitMessage(input);
          }}
        >
          <label className="composer-box">
            <span className="sr-only">Assistant message</span>
            <textarea
              rows={1}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="Ask anything about tickets, runbooks, or access requests"
            />
            <Button
              type="submit"
              variant="primary"
              disabled={!input.trim() || state === "loading"}
              icon={<Send size={15} aria-hidden="true" />}
            >
              Send
            </Button>
          </label>
          <div className="composer-foot">
            <span>
              <FileText size={12} aria-hidden="true" />
              Ticket API
            </span>
            <span>
              <Shield size={12} aria-hidden="true" />
              Session aware
            </span>
            <span>
              <Bot size={12} aria-hidden="true" />
              RAG citations
            </span>
          </div>
        </form>
      </div>
    </section>
  );
}

function defaultThreadId(user: ApiUser | null): string {
  return `user:${user?.id ?? "anonymous"}:default`;
}

function newThreadId(user: ApiUser | null): string {
  return `user:${user?.id ?? "anonymous"}:${generateId("chat")}`;
}

function threadStorageKey(user: ApiUser | null): string {
  return `capstone-its-chat-thread:${user?.id ?? "anonymous"}`;
}

function readThreadId(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function rememberThreadId(key: string, threadId: string) {
  try {
    window.localStorage.setItem(key, threadId);
  } catch {
    // Storage can be unavailable in private browsing modes.
  }
}

function defaultAssistantMessage(content = defaultAssistantText): ChatMessage {
  return {
    id: generateId("msg"),
    role: "assistant",
    content,
    createdAt: new Date().toISOString()
  };
}

function historyMessageToChatMessage(message: ChatHistoryMessage, index: number): ChatMessage {
  return {
    id: message.id || `history-${index}`,
    role: message.role,
    content: message.content,
    createdAt: message.created_at || new Date().toISOString()
  };
}
