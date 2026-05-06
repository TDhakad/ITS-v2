import {
  Activity,
  ArrowLeft,
  Bot,
  Bug,
  ChevronDown,
  ChevronRight,
  FileText,
  FolderKanban,
  HardDrive,
  LayoutDashboard,
  Link2,
  List,
  MessageSquare,
  Pencil,
  PanelLeftClose,
  PanelLeftOpen,
  Reply,
  SendHorizontal,
  Server,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  Trash2,
  X
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import { api } from "../api/client";
import {
  formatDateTime,
  formatTime,
  initials,
  STATUS_LABELS,
  ticketDescription,
  kbReferenceHref,
  parseTicketIdFromText,
  cx
} from "../lib";
import type { ApiUser, LoadState, ProjectSummary, Ticket, TicketComment, TicketInsight } from "../types";
import { Button, EmptyState, IconButton, LoadingState, PriorityBadge, StatusBadge } from "./common";
import { MarkdownContent } from "./MarkdownContent";

interface TicketDetailProps {
  ticket: Ticket;
  loadState: LoadState;
  error?: string | null;
  aiOpen: boolean;
  user: ApiUser | null;
  tickets: Ticket[];
  projects: ProjectSummary[];
  activeProjectId: number | null;
  onBack: () => void;
  onProjectChange: (projectId: number | null) => void;
  onViewChange: (view: "dashboard" | "assistant") => void;
  onToggleAi: () => void;
  onTicketSelect: (ticketId: number) => void;
}

export function TicketDetail({
  ticket,
  loadState,
  error,
  aiOpen,
  user,
  tickets,
  projects,
  activeProjectId,
  onBack,
  onProjectChange,
  onViewChange,
  onToggleAi,
  onTicketSelect
}: TicketDetailProps) {
  const [insight, setInsight] = useState<TicketInsight | null>(null);
  const [insightState, setInsightState] = useState<LoadState>("idle");
  const [insightError, setInsightError] = useState<string | null>(null);
  const [comments, setComments] = useState<TicketComment[]>([]);
  const [commentsState, setCommentsState] = useState<LoadState>("idle");
  const [commentsError, setCommentsError] = useState<string | null>(null);
  const [newCommentDraft, setNewCommentDraft] = useState("");
  const [replyTargetId, setReplyTargetId] = useState<number | null>(null);
  const [replyDrafts, setReplyDrafts] = useState<Record<number, string>>({});
  const [editingCommentId, setEditingCommentId] = useState<number | null>(null);
  const [editDrafts, setEditDrafts] = useState<Record<number, string>>({});
  const [commentSubmitting, setCommentSubmitting] = useState(false);
  const [conversationOpen, setConversationOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [insightWidth, setInsightWidth] = useState(352);
  const [isResizingDrawer, setIsResizingDrawer] = useState(false);
  const [viewportWidth, setViewportWidth] = useState<number>(() =>
    typeof window === "undefined" ? 1280 : window.innerWidth
  );
  const dragStateRef = useRef<{ startX: number; startWidth: number } | null>(null);

  const canResizeInsights = viewportWidth > 980;
  const counts = countTicketsForSidebar(tickets, user);

  useEffect(() => {
    function handleViewportResize() {
      setViewportWidth(window.innerWidth);
    }

    window.addEventListener("resize", handleViewportResize);
    return () => window.removeEventListener("resize", handleViewportResize);
  }, []);

  useEffect(() => {
    if (!isResizingDrawer) {
      return;
    }

    function onMouseMove(event: MouseEvent) {
      const dragState = dragStateRef.current;
      if (!dragState) {
        return;
      }
      const delta = dragState.startX - event.clientX;
      const maxWidth = Math.max(360, viewportWidth - (sidebarOpen ? 560 : 430));
      setInsightWidth(clamp(dragState.startWidth + delta, 280, maxWidth));
    }

    function onMouseUp() {
      dragStateRef.current = null;
      setIsResizingDrawer(false);
    }

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, [isResizingDrawer, sidebarOpen, viewportWidth]);

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

  useEffect(() => {
    let active = true;
    setComments([]);
    setCommentsError(null);
    setCommentsState("loading");
    setReplyTargetId(null);
    setReplyDrafts({});
    setEditingCommentId(null);
    setEditDrafts({});

    api
      .listTicketComments(ticket.id)
      .then((payload) => {
        if (!active) {
          return;
        }
        setComments(payload.comments ?? []);
        setCommentsState("ready");
      })
      .catch((caught: Error) => {
        if (!active) {
          return;
        }
        setCommentsError(caught.message);
        setCommentsState("error");
      });

    return () => {
      active = false;
    };
  }, [ticket.id]);

  useEffect(() => {
    setConversationOpen(false);
  }, [ticket.id]);

  const messages = ticket.conversation ?? ticket.messages ?? [];
  const threadedComments = useMemo(() => buildCommentTree(comments), [comments]);
  const isAdmin = user?.role === "admin";

  async function reloadComments() {
    const payload = await api.listTicketComments(ticket.id);
    setComments(payload.comments ?? []);
  }

  async function createComment(content: string, parentCommentId?: number | null) {
    const cleaned = content.trim();
    if (!cleaned || commentSubmitting) {
      return;
    }
    setCommentSubmitting(true);
    setCommentsError(null);
    try {
      await api.createTicketComment(ticket.id, {
        content: cleaned,
        parent_comment_id: parentCommentId ?? null,
      });
      await reloadComments();
      if (parentCommentId) {
        setReplyTargetId(null);
        setReplyDrafts((current) => ({ ...current, [parentCommentId]: "" }));
      } else {
        setNewCommentDraft("");
      }
    } catch (caught) {
      setCommentsError(caught instanceof Error ? caught.message : "Could not save comment.");
    } finally {
      setCommentSubmitting(false);
    }
  }

  async function saveEditedComment(commentId: number) {
    const cleaned = (editDrafts[commentId] ?? "").trim();
    if (!cleaned || commentSubmitting) {
      return;
    }
    setCommentSubmitting(true);
    setCommentsError(null);
    try {
      await api.updateTicketComment(ticket.id, commentId, { content: cleaned });
      await reloadComments();
      setEditingCommentId(null);
    } catch (caught) {
      setCommentsError(caught instanceof Error ? caught.message : "Could not update comment.");
    } finally {
      setCommentSubmitting(false);
    }
  }

  async function removeComment(commentId: number) {
    if (!isAdmin || commentSubmitting) {
      return;
    }
    const confirmed = window.confirm("Delete this comment and any replies?");
    if (!confirmed) {
      return;
    }
    setCommentSubmitting(true);
    setCommentsError(null);
    try {
      await api.deleteTicketComment(ticket.id, commentId);
      await reloadComments();
    } catch (caught) {
      setCommentsError(caught instanceof Error ? caught.message : "Could not delete comment.");
    } finally {
      setCommentSubmitting(false);
    }
  }

  function startDrawerResize(event: ReactMouseEvent<HTMLButtonElement>) {
    if (!canResizeInsights) {
      return;
    }
    event.preventDefault();
    dragStateRef.current = {
      startX: event.clientX,
      startWidth: insightWidth,
    };
    setIsResizingDrawer(true);
  }

  return (
    <section className={cx("ticket-detail-page", isResizingDrawer && "is-resizing")} aria-label="Ticket detail">
      <aside className={cx("ops-sidebar", "detail-sidebar", !sidebarOpen && "is-collapsed")}>
        <div className="detail-sidebar-header">
          <div className="ops-logo">
            <div className="ops-logo-icon">IT</div>
            <span className="ops-logo-name">Ops Console</span>
          </div>
          <IconButton
            className="detail-sidebar-toggle"
            onClick={() => setSidebarOpen((current) => !current)}
            aria-label={sidebarOpen ? "Collapse left sidebar" : "Expand left sidebar"}
            title={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
            type="button"
          >
            {sidebarOpen ? <PanelLeftClose size={15} aria-hidden="true" /> : <PanelLeftOpen size={15} aria-hidden="true" />}
          </IconButton>
        </div>

        <div className="ops-main-nav">
          <button className="ops-main-nav-item is-active" onClick={() => onViewChange("dashboard")}>
            <LayoutDashboard size={15} aria-hidden="true" />
            Dashboard
          </button>
          <button className="ops-main-nav-item" onClick={() => onViewChange("assistant")}>
            <Bot size={15} aria-hidden="true" />
            AI Assistant
          </button>
        </div>

        <div className="ops-divider" />

        <p className="ops-section-label">Projects</p>
        <div className="ops-section-list">
          {user?.role === "admin" ? (
            <button
              className={cx("ops-project-item", activeProjectId === null && "is-active")}
              onClick={() => {
                onProjectChange(null);
                onBack();
              }}
            >
              <span className="ops-project-dot" aria-hidden="true" />
              <span>All projects</span>
            </button>
          ) : null}
          {projects.map((project) => (
            <button
              className={cx("ops-project-item", activeProjectId === project.id && "is-active")}
              key={project.id}
              onClick={() => {
                onProjectChange(project.id);
                onBack();
              }}
            >
              <span className="ops-project-dot" aria-hidden="true" />
              <span>{project.name}</span>
            </button>
          ))}
        </div>

        <div className="ops-divider" />

        <p className="ops-section-label">Workspace</p>
        <div className="ops-section-list">
          <button className="ops-side-row is-active" onClick={onBack}>
            <span className="ops-side-left">
              <List size={14} aria-hidden="true" />
              <span>All tickets</span>
            </span>
            <span className="ops-side-count">{tickets.length}</span>
          </button>
          <button className="ops-side-row" onClick={onBack}>
            <span className="ops-side-left">
              <FolderKanban size={14} aria-hidden="true" />
              <span>My queue</span>
            </span>
            <span className="ops-side-count">{counts.mine}</span>
          </button>
          <button className="ops-side-row" onClick={onBack}>
            <span className="ops-side-left">
              <TrendingUp size={14} aria-hidden="true" />
              <span>Reports</span>
            </span>
          </button>
        </div>

        <div className="ops-divider" />

        <p className="ops-section-label">Categories</p>
        <div className="ops-section-list">
          <button className="ops-side-row" onClick={onBack}>
            <span className="ops-side-left">
              <Server size={14} aria-hidden="true" />
              <span>Infrastructure</span>
            </span>
            <span className="ops-side-count">{counts.byCategory.Infra ?? 0}</span>
          </button>
          <button className="ops-side-row" onClick={onBack}>
            <span className="ops-side-left">
              <Bug size={14} aria-hidden="true" />
              <span>Bugs</span>
            </span>
            <span className="ops-side-count">{counts.byCategory.Bug ?? 0}</span>
          </button>
          <button className="ops-side-row" onClick={onBack}>
            <span className="ops-side-left">
              <Activity size={14} aria-hidden="true" />
              <span>UI</span>
            </span>
            <span className="ops-side-count">{counts.byCategory.UI ?? 0}</span>
          </button>
          <button className="ops-side-row" onClick={onBack}>
            <span className="ops-side-left">
              <HardDrive size={14} aria-hidden="true" />
              <span>Hardware</span>
            </span>
            <span className="ops-side-count">{counts.byCategory.Hardware ?? 0}</span>
          </button>
        </div>

        <div className="ops-user-block">
          <strong>{user?.display_name ?? "User"}</strong>
          <small>{user?.email ?? "Not signed in"}</small>
        </div>
      </aside>

      <div className="ticket-detail-content">
        <div
          className={cx("ticket-detail-main", aiOpen && "is-shifted")}
          style={aiOpen && canResizeInsights ? { marginRight: `${insightWidth}px` } : undefined}
        >
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
                {(ticket.project_name || ticket.project_id) && (
                  <Meta label="Project" value={ticket.project_name ?? `Project #${ticket.project_id}`} />
                )}
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
              <button
                type="button"
                className="section-toggle"
                onClick={() => setConversationOpen((current) => !current)}
                aria-expanded={conversationOpen}
                aria-controls="ticket-conversation"
              >
                <span className="section-label">
                  <MessageSquare size={14} aria-hidden="true" />
                  Conversation
                </span>
                <span className="section-toggle-meta">
                  {messages.length} {messages.length === 1 ? "message" : "messages"}
                  {conversationOpen ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
                </span>
              </button>
              {conversationOpen ? (
                <div className="conversation-list" id="ticket-conversation">
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
                        <MarkdownContent
                          content={message.content}
                          onTicketSelect={onTicketSelect}
                        />
                      </article>
                    ))
                  ) : (
                    <p className="muted-text">No conversation messages are attached.</p>
                  )}
                </div>
              ) : null}
            </section>

            <section className="detail-section">
              <div className="section-label">
                <MessageSquare size={14} aria-hidden="true" />
                Comments
              </div>

              <div className="ticket-comment-composer">
                <textarea
                  value={newCommentDraft}
                  onChange={(event) => setNewCommentDraft(event.target.value)}
                  placeholder="Add a ticket comment"
                  rows={3}
                  maxLength={4000}
                />
                <div className="ticket-comment-actions">
                  <Button
                    className="compact-button"
                    icon={<SendHorizontal size={14} aria-hidden="true" />}
                    onClick={() => void createComment(newCommentDraft)}
                    disabled={commentSubmitting || !newCommentDraft.trim()}
                  >
                    Comment
                  </Button>
                </div>
              </div>

              {commentsError ? <p className="ticket-comment-error">{commentsError}</p> : null}

              {commentsState === "loading" ? (
                <p className="muted-text">Loading comments...</p>
              ) : commentsState === "error" ? (
                <p className="muted-text">Comments could not be loaded.</p>
              ) : threadedComments.length ? (
                <div className="ticket-comment-thread">
                  {threadedComments.map((comment) => (
                    <CommentNode
                      key={comment.id}
                      comment={comment}
                      depth={0}
                      isAdmin={Boolean(isAdmin)}
                      replyTargetId={replyTargetId}
                      replyDrafts={replyDrafts}
                      editingCommentId={editingCommentId}
                      editDrafts={editDrafts}
                      isBusy={commentSubmitting}
                      onReplyToggle={(commentId) =>
                        setReplyTargetId((current) => (current === commentId ? null : commentId))
                      }
                      onReplyDraftChange={(commentId, value) =>
                        setReplyDrafts((current) => ({ ...current, [commentId]: value }))
                      }
                      onReplySubmit={(commentId) => void createComment(replyDrafts[commentId] ?? "", commentId)}
                      onEditStart={(commentId, currentValue) => {
                        setEditingCommentId(commentId);
                        setEditDrafts((current) => ({ ...current, [commentId]: currentValue }));
                      }}
                      onEditCancel={() => setEditingCommentId(null)}
                      onEditDraftChange={(commentId, value) =>
                        setEditDrafts((current) => ({ ...current, [commentId]: value }))
                      }
                      onEditSave={(commentId) => void saveEditedComment(commentId)}
                      onDelete={(commentId) => void removeComment(commentId)}
                    />
                  ))}
                </div>
              ) : (
                <p className="muted-text">No comments yet.</p>
              )}
            </section>

            {ticket.linked_kb_articles.length ? (
              <section className="detail-section">
                <div className="section-label">
                  <Link2 size={14} aria-hidden="true" />
                  Knowledge references
                </div>
                <div className="reference-row">
                  {ticket.linked_kb_articles.slice(0, 4).map((reference) => {
                    const label = reference.title ?? reference.source ?? "Reference";
                    const href = kbReferenceHref(reference);
                    return href ? (
                      <a
                        className="reference-chip kb-link"
                        href={href}
                        target="_blank"
                        rel="noreferrer"
                        key={reference.kb_id ?? reference.title}
                      >
                        <Link2 size={10} aria-hidden="true" />
                        {label}
                      </a>
                    ) : (
                      <span className="reference-chip" key={reference.kb_id ?? reference.title}>
                        {label}
                      </span>
                    );
                  })}
                </div>
              </section>
            ) : null}

            {ticket.duplicate_ticket_ids.length ? (
              <section className="detail-section">
                <div className="section-label">
                  <FileText size={14} aria-hidden="true" />
                  Possible duplicates
                </div>
                <div className="reference-row">
                  {ticket.duplicate_ticket_ids.map((dupId) => (
                    <button
                      key={dupId}
                      className="reference-chip ticket-ref-chip"
                      onClick={() => onTicketSelect(dupId)}
                      type="button"
                    >
                      #{dupId}
                    </button>
                  ))}
                </div>
              </section>
            ) : null}
          </div>
        )}
        </div>
        <InsightsDrawer
          open={aiOpen}
          width={canResizeInsights ? insightWidth : undefined}
          resizable={canResizeInsights}
          resizing={isResizingDrawer}
          ticket={ticket}
          insight={insight}
          state={insightState}
          error={insightError}
          onClose={onToggleAi}
          onResizeStart={startDrawerResize}
          onTicketSelect={onTicketSelect}
        />
      </div>
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
  width,
  resizable,
  resizing,
  ticket,
  insight,
  state,
  error,
  onClose,
  onResizeStart,
  onTicketSelect
}: {
  open: boolean;
  width?: number;
  resizable: boolean;
  resizing: boolean;
  ticket: Ticket;
  insight: TicketInsight | null;
  state: LoadState;
  error: string | null;
  onClose: () => void;
  onResizeStart: (event: ReactMouseEvent<HTMLButtonElement>) => void;
  onTicketSelect: (id: number) => void;
}) {
  const fixes = insight?.suggested_fixes?.length
    ? insight.suggested_fixes
    : ["Review the ticket context and confirm the requester, scope, and affected system."];
  const signals = insight?.signals?.length ? insight.signals : [ticket.category, ticket.environment, ...ticket.keywords];

  return (
    <aside
      className={cx("insights-drawer", open && "is-open")}
      aria-label="AI insights"
      style={width ? { width: `${width}px` } : undefined}
    >
      {resizable ? (
        <button
          type="button"
          className={cx("drawer-resize-handle", resizing && "is-active")}
          aria-label="Resize AI insights panel"
          onMouseDown={onResizeStart}
        />
      ) : null}
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
            <MarkdownContent content={insight?.summary ?? ticket.summary} onTicketSelect={onTicketSelect} />
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
              {signals.slice(0, 8).map((signal) => {
                const maybeTicketId = parseTicketIdFromText(signal);
                if (maybeTicketId) {
                  return (
                    <button
                      key={signal}
                      className="reference-chip ticket-ref-chip"
                      onClick={() => onTicketSelect(maybeTicketId)}
                      type="button"
                    >
                      {signal}
                    </button>
                  );
                }
                return (
                  <span className="tag" key={signal}>
                    {signal}
                  </span>
                );
              })}
            </div>
          </DrawerBlock>
          <DrawerBlock title="References">
            {insight?.citations?.length ? (
              <div className="reference-stack">
                {insight.citations.slice(0, 3).map((reference) => {
                  const label = reference.title ?? reference.source ?? "Reference";
                  const href = kbReferenceHref(reference);
                  return href ? (
                    <a
                      className="reference-chip kb-link"
                      href={href}
                      target="_blank"
                      rel="noreferrer"
                      key={reference.kb_id ?? reference.title}
                    >
                      <Link2 size={10} aria-hidden="true" />
                      {label}
                    </a>
                  ) : (
                    <span className="reference-chip" key={reference.kb_id ?? reference.title}>
                      {label}
                    </span>
                  );
                })}
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

interface ThreadedComment extends TicketComment {
  children: ThreadedComment[];
}

function CommentNode({
  comment,
  depth,
  isAdmin,
  replyTargetId,
  replyDrafts,
  editingCommentId,
  editDrafts,
  isBusy,
  onReplyToggle,
  onReplyDraftChange,
  onReplySubmit,
  onEditStart,
  onEditCancel,
  onEditDraftChange,
  onEditSave,
  onDelete,
}: {
  comment: ThreadedComment;
  depth: number;
  isAdmin: boolean;
  replyTargetId: number | null;
  replyDrafts: Record<number, string>;
  editingCommentId: number | null;
  editDrafts: Record<number, string>;
  isBusy: boolean;
  onReplyToggle: (commentId: number) => void;
  onReplyDraftChange: (commentId: number, value: string) => void;
  onReplySubmit: (commentId: number) => void;
  onEditStart: (commentId: number, currentValue: string) => void;
  onEditCancel: () => void;
  onEditDraftChange: (commentId: number, value: string) => void;
  onEditSave: (commentId: number) => void;
  onDelete: (commentId: number) => void;
}) {
  const isReplying = replyTargetId === comment.id;
  const isEditing = editingCommentId === comment.id;
  const replyDraft = replyDrafts[comment.id] ?? "";
  const editDraft = editDrafts[comment.id] ?? comment.content;

  return (
    <div className={cx("ticket-comment-node", depth > 0 && "has-parent")} style={{ marginLeft: `${depth * 22}px` }}>
      <article className="ticket-comment-card">
        <header className="ticket-comment-header">
          <div className="ticket-comment-meta">
            <strong>{comment.author_display_name}</strong>
            <span>{formatTime(comment.created_at)}</span>
            {comment.edited ? <em>(edited)</em> : null}
          </div>
          <div className="ticket-comment-controls">
            <button
              className="ticket-comment-action"
              type="button"
              onClick={() => onReplyToggle(comment.id)}
              disabled={isBusy}
            >
              <Reply size={13} aria-hidden="true" /> Reply
            </button>
            {isAdmin ? (
              <>
                <button
                  className="ticket-comment-action"
                  type="button"
                  onClick={() => onEditStart(comment.id, comment.content)}
                  disabled={isBusy}
                >
                  <Pencil size={13} aria-hidden="true" /> Edit
                </button>
                <button
                  className="ticket-comment-action danger"
                  type="button"
                  onClick={() => onDelete(comment.id)}
                  disabled={isBusy}
                >
                  <Trash2 size={13} aria-hidden="true" /> Delete
                </button>
              </>
            ) : null}
          </div>
        </header>

        {isEditing ? (
          <div className="ticket-comment-editor">
            <textarea
              rows={3}
              value={editDraft}
              onChange={(event) => onEditDraftChange(comment.id, event.target.value)}
              maxLength={4000}
            />
            <div className="ticket-comment-actions">
              <Button
                className="compact-button"
                icon={<SendHorizontal size={14} aria-hidden="true" />}
                onClick={() => onEditSave(comment.id)}
                disabled={isBusy || !editDraft.trim()}
              >
                Save
              </Button>
              <Button className="compact-button" onClick={onEditCancel}>
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <p className="ticket-comment-content">{comment.content}</p>
        )}

        {isReplying ? (
          <div className="ticket-comment-editor">
            <textarea
              rows={2}
              value={replyDraft}
              onChange={(event) => onReplyDraftChange(comment.id, event.target.value)}
              maxLength={4000}
              placeholder="Write a reply"
            />
            <div className="ticket-comment-actions">
              <Button
                className="compact-button"
                icon={<SendHorizontal size={14} aria-hidden="true" />}
                onClick={() => onReplySubmit(comment.id)}
                disabled={isBusy || !replyDraft.trim()}
              >
                Reply
              </Button>
              <Button className="compact-button" onClick={() => onReplyToggle(comment.id)}>
                Cancel
              </Button>
            </div>
          </div>
        ) : null}
      </article>

      {comment.children.length ? (
        <div className="ticket-comment-children">
          {comment.children.map((child) => (
            <CommentNode
              key={child.id}
              comment={child}
              depth={depth + 1}
              isAdmin={isAdmin}
              replyTargetId={replyTargetId}
              replyDrafts={replyDrafts}
              editingCommentId={editingCommentId}
              editDrafts={editDrafts}
              isBusy={isBusy}
              onReplyToggle={onReplyToggle}
              onReplyDraftChange={onReplyDraftChange}
              onReplySubmit={onReplySubmit}
              onEditStart={onEditStart}
              onEditCancel={onEditCancel}
              onEditDraftChange={onEditDraftChange}
              onEditSave={onEditSave}
              onDelete={onDelete}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function buildCommentTree(comments: TicketComment[]): ThreadedComment[] {
  const byId = new Map<number, ThreadedComment>();
  const roots: ThreadedComment[] = [];

  for (const comment of comments) {
    byId.set(comment.id, { ...comment, children: [] });
  }

  for (const comment of comments) {
    const node = byId.get(comment.id);
    if (!node) {
      continue;
    }
    if (comment.parent_comment_id && byId.has(comment.parent_comment_id)) {
      byId.get(comment.parent_comment_id)?.children.push(node);
    } else {
      roots.push(node);
    }
  }

  return roots;
}

function countTicketsForSidebar(tickets: Ticket[], user: ApiUser | null) {
  return tickets.reduce(
    (acc, current) => {
      if (user && current.user_id === String(user.id)) {
        acc.mine += 1;
      }
      acc.byCategory[current.category] = (acc.byCategory[current.category] ?? 0) + 1;
      return acc;
    },
    {
      mine: 0,
      byCategory: {} as Partial<Record<Ticket["category"], number>>,
    }
  );
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum);
}
