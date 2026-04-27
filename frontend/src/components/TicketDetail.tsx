import {
  ArrowLeft,
  FileText,
  Link2,
  MessageSquare,
  ShieldCheck,
  Sparkles,
  X
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api/client";
import {
  formatDateTime,
  formatTime,
  initials,
  STATUS_LABELS,
  ticketDescription,
  cx
} from "../lib";
import type { LoadState, Ticket, TicketInsight } from "../types";
import { Button, EmptyState, IconButton, LoadingState, PriorityBadge, StatusBadge } from "./common";
import { MarkdownContent } from "./MarkdownContent";

interface TicketDetailProps {
  ticket: Ticket;
  loadState: LoadState;
  error?: string | null;
  aiOpen: boolean;
  onBack: () => void;
  onToggleAi: () => void;
}

export function TicketDetail({
  ticket,
  loadState,
  error,
  aiOpen,
  onBack,
  onToggleAi
}: TicketDetailProps) {
  const [insight, setInsight] = useState<TicketInsight | null>(null);
  const [insightState, setInsightState] = useState<LoadState>("idle");
  const [insightError, setInsightError] = useState<string | null>(null);

  useEffect(() => {
    if (!aiOpen) {
      return;
    }

    let active = true;
    setInsight(null);
    setInsightState("loading");
    setInsightError(null);

    api
      .getTicketInsights(ticket.id)
      .then((data) => {
        if (active) {
          setInsight(data);
          setInsightState("ready");
        }
      })
      .catch((caught: Error) => {
        if (active) {
          setInsightError(caught.message);
          setInsightState("error");
        }
      });

    return () => {
      active = false;
    };
  }, [aiOpen, ticket.id]);

  const messages = ticket.conversation ?? ticket.messages ?? [];

  return (
    <section className="ticket-detail-page" aria-label="Ticket detail">
      <div className={cx("ticket-detail-main", aiOpen && "is-shifted")}>
        <header className="detail-topbar">
          <button className="back-button" onClick={onBack}>
            <ArrowLeft size={15} aria-hidden="true" />
            Back
          </button>
          <div className="breadcrumb">
            All tickets <span>/</span> <strong>#{ticket.id}</strong>
          </div>
          <div className="detail-actions">
            <Button
              className={cx("ai-toggle", aiOpen && "is-active")}
              icon={<Sparkles size={15} aria-hidden="true" />}
              onClick={onToggleAi}
            >
              AI Insights
            </Button>
            <select className="status-select" value={ticket.status} disabled aria-label="Ticket status">
              {Object.entries(STATUS_LABELS).map(([value, label]) => (
                <option value={value} key={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
        </header>

        {loadState === "loading" ? (
          <LoadingState label="Loading ticket" />
        ) : loadState === "error" ? (
          <EmptyState title="Ticket did not load">{error}</EmptyState>
        ) : (
          <div className="detail-body">
            <section className="ticket-title-block">
              <h1>{ticket.title || ticket.summary}</h1>
              <div className="meta-grid">
                <Meta label="Requester" value={`${ticket.requester} / #${ticket.id}`} />
                <Meta label="Priority" value={<PriorityBadge priority={ticket.priority} />} />
                <Meta label="Status" value={<StatusBadge status={ticket.status} />} />
                <Meta label="Category" value={`${ticket.category} / ${ticket.environment}`} />
                <Meta label="Updated" value={formatDateTime(ticket.updated_at)} />
              </div>
            </section>

            <section className="detail-section">
              <div className="section-label">
                <FileText size={14} aria-hidden="true" />
                Description
              </div>
              <p className="description-text">{ticketDescription(ticket)}</p>
            </section>

            <section className="detail-section">
              <div className="section-label">
                <MessageSquare size={14} aria-hidden="true" />
                Conversation
              </div>
              <div className="conversation-list">
                {messages.length ? (
                  messages.map((message, index) => (
                    <article
                      key={`${message.role}-${message.created_at ?? index}`}
                      className={cx(
                        "conversation-message",
                        message.role === "user" && "user",
                        message.role === "assistant" && "assistant",
                        message.role === "system" && "system"
                      )}
                    >
                      <div className="message-heading">
                        {message.role}
                        <span>{formatTime(message.created_at)}</span>
                      </div>
                      <MarkdownContent content={message.content} />
                    </article>
                  ))
                ) : (
                  <p className="muted-text">No conversation messages are attached.</p>
                )}
              </div>
            </section>

            {ticket.linked_kb_articles.length ? (
              <section className="detail-section">
                <div className="section-label">
                  <Link2 size={14} aria-hidden="true" />
                  Knowledge references
                </div>
                <div className="reference-row">
                  {ticket.linked_kb_articles.slice(0, 4).map((reference) => (
                    <span className="reference-chip" key={reference.kb_id ?? reference.title}>
                      {reference.title ?? reference.source}
                    </span>
                  ))}
                </div>
              </section>
            ) : null}
          </div>
        )}
      </div>
      <InsightsDrawer
        open={aiOpen}
        ticket={ticket}
        insight={insight}
        state={insightState}
        error={insightError}
        onClose={onToggleAi}
      />
    </section>
  );
}

function Meta({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="meta-field">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function InsightsDrawer({
  open,
  ticket,
  insight,
  state,
  error,
  onClose
}: {
  open: boolean;
  ticket: Ticket;
  insight: TicketInsight | null;
  state: LoadState;
  error: string | null;
  onClose: () => void;
}) {
  const fixes = insight?.suggested_fixes?.length
    ? insight.suggested_fixes
    : ["Review the ticket context and confirm the requester, scope, and affected system."];
  const signals = insight?.signals?.length ? insight.signals : [ticket.category, ticket.environment, ...ticket.keywords];

  return (
    <aside className={cx("insights-drawer", open && "is-open")} aria-label="AI insights">
      <div className="drawer-header">
        <span className="live-dot" />
        <strong>AI insights</strong>
        <IconButton aria-label="Close AI insights" onClick={onClose}>
          <X size={14} aria-hidden="true" />
        </IconButton>
      </div>
      {state === "loading" ? (
        <LoadingState label="Loading insights" />
      ) : state === "error" ? (
        <EmptyState title="Insights unavailable">{error}</EmptyState>
      ) : (
        <div className="drawer-body">
          <DrawerBlock title="Summary">
            <MarkdownContent content={insight?.summary ?? ticket.summary} />
          </DrawerBlock>
          <DrawerBlock title="Recommended actions">
            <div className="action-list">
              {fixes.slice(0, 4).map((fix) => (
                <div className="action-row" key={fix}>
                  <span className="action-icon">
                    <ShieldCheck size={14} aria-hidden="true" />
                  </span>
                  <span>{fix}</span>
                </div>
              ))}
            </div>
          </DrawerBlock>
          <DrawerBlock title="Signals">
            <div className="tag-row">
              {signals.slice(0, 8).map((signal) => (
                <span className="tag" key={signal}>
                  {signal}
                </span>
              ))}
            </div>
          </DrawerBlock>
          <DrawerBlock title="References">
            {insight?.citations?.length ? (
              <div className="reference-stack">
                {insight.citations.slice(0, 3).map((reference) => (
                  <span className="reference-chip" key={reference.kb_id ?? reference.title}>
                    {reference.title ?? reference.source}
                  </span>
                ))}
              </div>
            ) : (
              <p>No knowledge references returned.</p>
            )}
          </DrawerBlock>
          <DrawerBlock title="Suggested assignee">
            <div className="assignee-row">
              <span className="avatar soft">{initials(ticket.category, "IT")}</span>
              <span>{ticket.category === "Infra" ? "Infrastructure team" : "Helpdesk queue"}</span>
            </div>
          </DrawerBlock>
        </div>
      )}
    </aside>
  );
}

function DrawerBlock({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="drawer-block">
      <h2>{title}</h2>
      <div>{children}</div>
    </section>
  );
}
