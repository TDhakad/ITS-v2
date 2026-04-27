import { useState, type FormEvent } from "react";
import { X } from "lucide-react";
import { api } from "../api/client";
import type { ApiUser, Environment, LoadState, Ticket, TicketCategory } from "../types";
import { Button, FieldError, IconButton } from "./common";

interface NewTicketModalProps {
  open: boolean;
  user: ApiUser | null;
  onClose: () => void;
  onCreated: (ticket: Ticket) => void;
}

const categories: TicketCategory[] = ["Infra", "Bug", "UI", "Hardware", "Feature"];
const environments: Environment[] = ["unknown", "development", "staging", "production"];
const priorities = ["Low", "Medium", "High", "Critical"] as const;
type PriorityLabel = (typeof priorities)[number];

export function NewTicketModal({ open, user, onClose, onCreated }: NewTicketModalProps) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState<TicketCategory>("Infra");
  const [priority, setPriority] = useState<PriorityLabel>("Medium");
  const [environment, setEnvironment] = useState<Environment>("unknown");
  const [appName, setAppName] = useState("");
  const [keywords, setKeywords] = useState("");
  const [state, setState] = useState<LoadState>("idle");
  const [error, setError] = useState<string | null>(null);

  if (!open) {
    return null;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setState("loading");
    setError(null);

    try {
      const response = await api.createTicket({
        title: title.trim() || undefined,
        description: description.trim(),
        user_id: user ? String(user.id) : "anonymous",
        thread_id: `manual-${Date.now()}`,
        app_name: appName.trim() || null,
        environment,
        clearance: user?.clearance ?? "public",
        category,
        priority,
        keywords: keywords
          .split(",")
          .map((keyword) => keyword.trim())
          .filter(Boolean)
          .slice(0, 12),
        metadata: { source: "react-ui" }
      });
      onCreated(response.ticket);
      setTitle("");
      setDescription("");
      setCategory("Infra");
      setPriority("Medium");
      setEnvironment("unknown");
      setAppName("");
      setKeywords("");
      setState("ready");
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Ticket creation failed.");
      setState("error");
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal wide" role="dialog" aria-modal="true" aria-labelledby="new-ticket-title">
        <header className="modal-header">
          <h2 id="new-ticket-title">New ticket</h2>
          <IconButton aria-label="Close new ticket" onClick={onClose}>
            <X size={16} aria-hidden="true" />
          </IconButton>
        </header>
        <form className="stack-form" onSubmit={submit}>
          <label>
            <span>Title</span>
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              maxLength={300}
              placeholder="Short summary"
            />
          </label>
          <label>
            <span>Description</span>
            <textarea
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              minLength={1}
              maxLength={8000}
              rows={5}
              required
            />
          </label>
          <div className="form-grid">
            <label>
              <span>Category</span>
              <select value={category} onChange={(event) => setCategory(event.target.value as TicketCategory)}>
                {categories.map((value) => (
                  <option value={value} key={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Priority</span>
              <select value={priority} onChange={(event) => setPriority(event.target.value as PriorityLabel)}>
                {priorities.map((value) => (
                  <option value={value} key={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Environment</span>
              <select
                value={environment}
                onChange={(event) => setEnvironment(event.target.value as Environment)}
              >
                {environments.map((value) => (
                  <option value={value} key={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Application</span>
              <input
                value={appName}
                onChange={(event) => setAppName(event.target.value)}
                maxLength={120}
              />
            </label>
          </div>
          <label>
            <span>Keywords</span>
            <input
              value={keywords}
              onChange={(event) => setKeywords(event.target.value)}
              placeholder="aws, staging, access"
            />
          </label>
          <FieldError message={error} />
          <div className="modal-actions">
            <Button onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={state === "loading" || !description.trim()}>
              {state === "loading" ? "Creating" : "Create ticket"}
            </Button>
          </div>
        </form>
      </section>
    </div>
  );
}
