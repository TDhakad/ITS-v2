const state = {
  tickets: [],
  filteredTickets: [],
  selectedTicketId: null,
};

const ticketList = document.querySelector("#ticket-list");
const queueCount = document.querySelector("#queue-count");
const refreshButton = document.querySelector("#refresh-button");
const ticketFilters = document.querySelector("#ticket-filters");
const searchInput = document.querySelector("#ticket-search");
const statusFilter = document.querySelector("#status-filter");
const priorityFilter = document.querySelector("#priority-filter");
const detailTitle = document.querySelector("#detail-title");
const detailStatus = document.querySelector("#detail-status");
const detailRequester = document.querySelector("#detail-requester");
const detailPriority = document.querySelector("#detail-priority");
const detailCategory = document.querySelector("#detail-category");
const detailUpdated = document.querySelector("#detail-updated");
const detailDescription = document.querySelector("#detail-description");
const detailTimeline = document.querySelector("#detail-timeline");
const insightsStatus = document.querySelector("#insights-status");
const insightSummary = document.querySelector("#insight-summary");
const insightAction = document.querySelector("#insight-action");
const insightSignals = document.querySelector("#insight-signals");
const insightCitations = document.querySelector("#insight-citations");

function normalizeText(value, fallback = "-") {
  if (typeof value === "string") {
    return value.trim() || fallback;
  }
  if (value === null || value === undefined) {
    return fallback;
  }
  return String(value);
}

function pick(obj, keys, fallback = undefined) {
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

function getTicketId(ticket) {
  return normalizeText(pick(ticket, ["id", "ticket_id", "number"], ""), "");
}

function normalizeTickets(payload) {
  if (Array.isArray(payload)) {
    return payload;
  }
  for (const key of ["tickets", "items", "results", "data"]) {
    if (Array.isArray(payload?.[key])) {
      return payload[key];
    }
  }
  return [];
}

function formatDate(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return normalizeText(value);
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function statusClass(priority) {
  return `badge-${normalizeText(priority, "").toLowerCase().replaceAll(" ", "_")}`;
}

function renderEmpty(message) {
  ticketList.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.textContent = message;
  ticketList.append(empty);
}

function renderTickets(tickets) {
  ticketList.replaceChildren();
  queueCount.textContent = `${tickets.length} shown`;

  if (!tickets.length) {
    renderEmpty("No tickets match the current filters.");
    return;
  }

  for (const ticket of tickets) {
    const id = getTicketId(ticket);
    const title = normalizeText(pick(ticket, ["title", "summary", "subject"], "Untitled ticket"));
    const requester = normalizeText(pick(ticket, ["requester", "requester_email", "user_email", "created_by"], "Unknown"));
    const status = normalizeText(pick(ticket, ["status", "state"], "open"));
    const priority = normalizeText(pick(ticket, ["priority", "severity"], "medium"));
    const updated = normalizeText(pick(ticket, ["updated_at", "modified_at", "created_at"], ""), "");

    const button = document.createElement("button");
    button.className = "ticket-row";
    button.type = "button";
    button.dataset.ticketId = id;
    button.classList.toggle("is-selected", id === state.selectedTicketId);
    button.setAttribute("aria-pressed", id === state.selectedTicketId ? "true" : "false");

    const main = document.createElement("div");
    const titleEl = document.createElement("p");
    titleEl.className = "ticket-title";
    titleEl.textContent = title;

    const meta = document.createElement("div");
    meta.className = "ticket-meta";
    meta.textContent = `${requester} | #${id || "pending"}`;

    main.append(titleEl, meta);

    const side = document.createElement("div");
    side.className = "ticket-submeta";

    const priorityBadge = document.createElement("span");
    priorityBadge.className = `badge ${statusClass(priority)}`;
    priorityBadge.textContent = priority;

    const statusBadge = document.createElement("span");
    statusBadge.className = "badge";
    statusBadge.textContent = status;

    const updatedEl = document.createElement("span");
    updatedEl.textContent = formatDate(updated);

    side.append(priorityBadge, statusBadge, updatedEl);
    button.append(main, side);
    button.addEventListener("click", () => selectTicket(id));
    ticketList.append(button);
  }
}

function applyFilters() {
  const query = searchInput.value.trim().toLowerCase();
  const status = statusFilter.value;
  const priority = priorityFilter.value;

  state.filteredTickets = state.tickets.filter((ticket) => {
    const haystack = [
      getTicketId(ticket),
      pick(ticket, ["title", "summary", "subject"], ""),
      pick(ticket, ["requester", "requester_email", "user_email", "created_by"], ""),
      pick(ticket, ["category", "type"], ""),
    ]
      .join(" ")
      .toLowerCase();

    const ticketStatus = normalizeText(pick(ticket, ["status", "state"], ""), "").toLowerCase();
    const ticketPriority = normalizeText(pick(ticket, ["priority", "severity"], ""), "").toLowerCase();

    return (
      (!query || haystack.includes(query)) &&
      (!status || ticketStatus === status) &&
      (!priority || ticketPriority === priority)
    );
  });

  renderTickets(state.filteredTickets);
}

function setDetailLoading() {
  detailTitle.textContent = "Loading ticket";
  detailStatus.textContent = "Loading";
  detailStatus.classList.add("is-active");
  detailStatus.classList.remove("is-error");
  detailRequester.textContent = "-";
  detailPriority.textContent = "-";
  detailCategory.textContent = "-";
  detailUpdated.textContent = "-";
  detailDescription.textContent = "Loading...";
  detailTimeline.replaceChildren();
}

function renderTimeline(ticket) {
  detailTimeline.replaceChildren();
  const messages = pick(ticket, ["messages", "conversation", "events", "notes"], []);

  if (!Array.isArray(messages) || !messages.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No conversation history returned.";
    detailTimeline.append(empty);
    return;
  }

  for (const message of messages) {
    const item = document.createElement("article");
    item.className = "timeline-item";

    const meta = document.createElement("p");
    meta.className = "timeline-meta";
    meta.textContent = [
      normalizeText(pick(message, ["role", "author", "type"], "note")),
      formatDate(pick(message, ["created_at", "timestamp", "time"], "")),
    ].join(" | ");

    const body = document.createElement("p");
    body.className = "preserve-lines";
    body.textContent = normalizeText(pick(message, ["content", "message", "text", "body"], ""));

    item.append(meta, body);
    detailTimeline.append(item);
  }
}

function renderTicketDetail(ticket) {
  const status = normalizeText(pick(ticket, ["status", "state"], "open"));
  detailTitle.textContent = normalizeText(pick(ticket, ["title", "summary", "subject"], "Untitled ticket"));
  detailStatus.textContent = status;
  detailStatus.classList.remove("is-active", "is-error");
  detailRequester.textContent = normalizeText(pick(ticket, ["requester", "requester_email", "user_email", "created_by"], "Unknown"));
  detailPriority.textContent = normalizeText(pick(ticket, ["priority", "severity"], "Unassigned"));
  detailCategory.textContent = normalizeText(pick(ticket, ["category", "type"], "Unclassified"));
  detailUpdated.textContent = formatDate(pick(ticket, ["updated_at", "modified_at", "created_at"], ""));
  detailDescription.textContent = normalizeText(
    pick(ticket, ["description", "body", "details", "initial_message"], "No description returned."),
  );
  renderTimeline(ticket);
}

function normalizeInsightList(value) {
  if (Array.isArray(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    return [value.trim()];
  }
  return [];
}

function renderList(element, values, emptyText) {
  element.replaceChildren();
  if (!values.length) {
    const item = document.createElement("li");
    item.textContent = emptyText;
    element.append(item);
    return;
  }

  for (const value of values.slice(0, 8)) {
    const item = document.createElement("li");
    item.textContent = normalizeText(value);
    element.append(item);
  }
}

function renderInsightCitations(citations) {
  insightCitations.replaceChildren();
  if (!citations.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No references returned.";
    insightCitations.append(empty);
    return;
  }

  for (const citation of citations.slice(0, 5)) {
    const item = document.createElement("article");
    item.className = "citation";

    const title = document.createElement("p");
    title.className = "citation-title";
    title.textContent = normalizeText(pick(citation, ["title", "source", "document", "name"], "Knowledge source"));

    const text = document.createElement("p");
    text.className = "citation-text";
    text.textContent = normalizeText(pick(citation, ["snippet", "content", "text", "summary"], "Reference matched this ticket."));

    item.append(title, text);
    insightCitations.append(item);
  }
}

function renderInsights(insights) {
  insightsStatus.classList.remove("is-active", "is-error");
  insightSummary.textContent = normalizeText(
    pick(insights, ["summary", "ticket_summary", "analysis"], "No summary returned."),
  );
  insightAction.textContent = normalizeText(
    pick(insights, ["recommended_action", "action", "next_step", "recommendation"], "-"),
  );
  renderList(
    insightSignals,
    normalizeInsightList(pick(insights, ["signals", "risk_signals", "tags", "labels"], [])),
    "No signals returned",
  );
  renderInsightCitations(normalizeInsightList(pick(insights, ["citations", "sources", "references"], [])));
}

async function fetchJson(url) {
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`${url} failed with ${response.status}`);
  }
  return response.json();
}

async function loadTickets() {
  refreshButton.disabled = true;
  renderEmpty("Loading tickets...");
  try {
    const payload = await fetchJson("/api/tickets");
    state.tickets = normalizeTickets(payload);
    if (!state.selectedTicketId && state.tickets.length) {
      state.selectedTicketId = getTicketId(state.tickets[0]);
    }
    applyFilters();
    if (state.selectedTicketId) {
      await selectTicket(state.selectedTicketId);
    }
  } catch (error) {
    renderEmpty("Could not load tickets.");
    queueCount.textContent = "0 shown";
    console.error(error);
  } finally {
    refreshButton.disabled = false;
  }
}

async function selectTicket(id) {
  if (!id) {
    return;
  }

  state.selectedTicketId = id;
  renderTickets(state.filteredTickets);
  setDetailLoading();
  insightsStatus.classList.add("is-active");
  insightsStatus.classList.remove("is-error");
  insightSummary.textContent = "Loading...";
  insightAction.textContent = "-";
  renderList(insightSignals, [], "Loading signals...");
  insightCitations.replaceChildren();

  try {
    const [ticket, insights] = await Promise.all([
      fetchJson(`/api/tickets/${encodeURIComponent(id)}`),
      fetchJson(`/api/tickets/${encodeURIComponent(id)}/insights`),
    ]);
    renderTicketDetail(ticket);
    renderInsights(insights);
  } catch (error) {
    detailStatus.textContent = "Load error";
    detailStatus.classList.remove("is-active");
    detailStatus.classList.add("is-error");
    insightsStatus.classList.remove("is-active");
    insightsStatus.classList.add("is-error");
    insightSummary.textContent = "Could not load ticket insights.";
    console.error(error);
  }
}

refreshButton.addEventListener("click", loadTickets);
ticketFilters.addEventListener("submit", (event) => event.preventDefault());
searchInput.addEventListener("input", applyFilters);
statusFilter.addEventListener("change", applyFilters);
priorityFilter.addEventListener("change", applyFilters);

loadTickets();
