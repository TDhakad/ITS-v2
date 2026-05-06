from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.llm import get_chat_model
from app.schemas import CommentClassification

logger = logging.getLogger(__name__)

COMMENT_CLASSIFICATION_PROMPT = """You are a ticket comment analyzer. Extract structured metadata from the comment below.

TICKET CONTEXT:
Title: {ticket_title}
Description: {ticket_description}

COMMENT:
Author: {author}
Timestamp: {timestamp}
Content: {comment_text}

Return ONLY valid JSON. No explanation.

{
  "tags": [],           // Pick 1-3: detail | status_update | rca_clue | blocker | ticket_ref | noise
  "references_tickets": [],     // Ticket IDs explicitly or implicitly mentioned (e.g. "T-123", "that auth issue")
  "references_systems": [],     // Services, components, or systems mentioned
  "references_people": [],      // @mentions or names of people referenced
  "signal_strength": 0.0,       // 0.0-1.0: how useful is this comment for future diagnosis
  "summary": ""                 // One sentence. What does this comment actually say?
}

TAGGING RULES:
- rca_clue: contains a cause, root cause hint, or "because / caused by / due to" language
- blocker: mentions something preventing progress, with or without a ticket reference
- ticket_ref: references another ticket or issue, even informally
- status_update: progress report, resolution note, or state change
- detail: adds context to the description with no causal or relational signal
- noise: greetings, acknowledgements, "+1", or no informational value

signal_strength guide:
- 0.8-1.0: directly explains a cause, names a system, or links tickets
- 0.4-0.7: adds useful context but is not decisive
- 0.0-0.3: noise or purely administrative"""

_TICKET_ID_RE = re.compile(r"\bT-\d+\b", re.IGNORECASE)
_MENTION_RE = re.compile(r"@([a-zA-Z0-9._-]+)")
_SYSTEM_RE = re.compile(
    r"\b([a-z0-9][a-z0-9-]*(?:service|gateway|worker|api|db|backend|frontend))\b",
    re.IGNORECASE,
)


def classify_comment_metadata(
    *,
    ticket_title: str,
    ticket_description: str,
    author: str,
    timestamp: datetime | str,
    comment_text: str,
) -> CommentClassification:
    prompt = _render_comment_classification_prompt(
        ticket_title=ticket_title.strip() or "(unknown)",
        ticket_description=ticket_description.strip() or "(none)",
        author=author.strip() or "(unknown)",
        timestamp=(
            timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
        ),
        comment_text=comment_text.strip(),
    )
    model = get_chat_model()

    # First attempt: schema-constrained structured output.
    # if hasattr(model, "with_structured_output"):
    #     try:
    #         structured_model = model.with_structured_output(CommentClassification)
    #         output = structured_model.invoke(prompt)
    #         if output is not None:
    #             if isinstance(output, CommentClassification):
    #                 return _normalize_classification(output, comment_text)
    #             parsed = CommentClassification.model_validate(output)
    #             return _normalize_classification(parsed, comment_text)
    #         logger.warning(
    #             "Structured comment classification returned None; falling back to JSON parse mode."
    #         )
    #     except Exception as structured_exc:
    #         logger.warning(
    #             "Structured comment classification failed; falling back to JSON parse mode: %s",
    #             structured_exc,
    #         )

    # Second attempt: plain invoke with strict JSON output contract.
    try:
        raw = model.invoke(
            [
                SystemMessage(
                    content=(
                        "Extract structured metadata for a ticket comment. "
                        "Return only strict JSON matching the requested schema."
                    )
                ),
                HumanMessage(content=prompt),
            ]
        )
        content = raw.content if isinstance(raw.content, str) else str(raw.content)
        payload = _parse_json_payload(content)
        parsed = CommentClassification.model_validate(payload)
        return _normalize_classification(parsed, comment_text)
    except Exception as exc:
        logger.warning("Comment classification failed, using fallback: %s", exc)
        return _heuristic_classification(comment_text)


def _render_comment_classification_prompt(
    *,
    ticket_title: str,
    ticket_description: str,
    author: str,
    timestamp: str,
    comment_text: str,
) -> str:
    # Use explicit token replacement so literal JSON braces remain untouched.
    prompt = COMMENT_CLASSIFICATION_PROMPT
    replacements = {
        "ticket_title": ticket_title,
        "ticket_description": ticket_description,
        "author": author,
        "timestamp": timestamp,
        "comment_text": comment_text,
    }
    for key, value in replacements.items():
        prompt = prompt.replace(f"{{{key}}}", value)
    return prompt


def _parse_json_payload(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(candidate[start : end + 1])

    if not isinstance(parsed, dict):
        raise ValueError("Classifier output must be a JSON object")
    return parsed


def _normalize_classification(
    classification: CommentClassification,
    comment_text: str,
) -> CommentClassification:
    tags = list(dict.fromkeys(classification.tags))[:3]
    summary = classification.summary.strip()
    if not summary:
        summary = _summary_from_text(comment_text)

    return CommentClassification(
        tags=tags,
        references_tickets=classification.references_tickets,
        references_systems=classification.references_systems,
        references_people=classification.references_people,
        signal_strength=max(0.0, min(float(classification.signal_strength), 1.0)),
        summary=summary,
    )


def _heuristic_classification(comment_text: str) -> CommentClassification:
    text = comment_text.strip()
    lowered = text.casefold()

    references_tickets = list(
        dict.fromkeys(match.upper() for match in _TICKET_ID_RE.findall(text))
    )
    references_people = [f"@{name}" for name in _MENTION_RE.findall(text)]
    references_people = list(dict.fromkeys(references_people))
    references_systems = [match.lower() for match in _SYSTEM_RE.findall(text)]
    references_systems = list(dict.fromkeys(references_systems))

    tags: list[str] = []
    if references_tickets:
        tags.append("ticket_ref")
    if any(
        token in lowered
        for token in ("because", "caused by", "due to", "root cause", "race condition")
    ):
        tags.append("rca_clue")
    if any(
        token in lowered
        for token in ("blocked", "blocking", "waiting on", "cannot", "can't", "unable")
    ):
        tags.append("blocker")
    if any(
        token in lowered
        for token in ("fixed", "resolved", "deployed", "done", "in progress", "updated")
    ):
        tags.append("status_update")
    if not tags and len(text) <= 20:
        tags.append("noise")
    if not tags:
        tags.append("detail")

    if tags == ["noise"]:
        signal_strength = 0.1
    elif "rca_clue" in tags:
        signal_strength = 0.85
    elif "blocker" in tags or references_tickets or references_systems:
        signal_strength = 0.65
    else:
        signal_strength = 0.45

    return CommentClassification(
        tags=tags[:3],
        references_tickets=references_tickets,
        references_systems=references_systems,
        references_people=references_people,
        signal_strength=signal_strength,
        summary=_summary_from_text(text),
    )


def _summary_from_text(text: str) -> str:
    clean = " ".join(text.split())
    if not clean:
        return "No diagnostic detail provided."

    parts = re.split(r"(?<=[.!?])\s+", clean, maxsplit=1)
    sentence = parts[0].strip()
    if not sentence:
        sentence = clean
    if len(sentence) > 240:
        return f"{sentence[:240].rstrip()}..."
    return sentence
