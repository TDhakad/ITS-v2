import { Children, isValidElement, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cx, parseTicketIdFromHref, parseTicketIdFromText } from "../lib";

// Matches: "Ticket #42", "ticket #42", "#42" — word-boundary safe.
const TICKET_REF_RE = /(?:ticket\s+)?#(\d+)/gi;

/**
 * Rewrites Ticket #N references in plain text so ReactMarkdown can render
 * them as clickable anchor elements via the custom `a` renderer.
 * Existing markdown links are left untouched.
 */
function injectTicketLinks(content: string): string {
  // Split on already-existing markdown links/images so we only rewrite plain text.
  const parts = content.split(/(\[.*?\](?:\(.*?\)|\[.*?\])|!\[.*?\]\(.*?\))/);
  return parts
    .map((part, i) =>
      // Odd indices are already-existing markdown constructs — leave them alone.
      i % 2 === 1 ? part : part.replace(TICKET_REF_RE, (_, id) => `[#${id}](ticket://${id})`)
    )
    .join("");
}

function buildComponents(onTicketSelect?: (id: number) => void): Components {
  return {
    a: ({ node: _node, href, children, ...rest }) => {
      const ticketId = parseTicketIdFromHref(href)
        ?? (isLocalHref(href) ? parseTicketIdFromText(childrenText(children)) : null);
      if (ticketId && onTicketSelect) {
        return (
          <button
            className="inline-ticket-link"
            onClick={() => onTicketSelect(ticketId)}
            type="button"
          >
            {children}
          </button>
        );
      }
      return (
        <a href={href} target="_blank" rel="noreferrer" {...rest}>
          {children}
        </a>
      );
    },
  };
}

function childrenText(children: ReactNode): string {
  return Children.toArray(children)
    .map((child) => {
      if (typeof child === "string" || typeof child === "number") {
        return String(child);
      }
      if (isValidElement<{ children?: ReactNode }>(child)) {
        return childrenText(child.props.children ?? "");
      }
      return "";
    })
    .join(" ");
}

function isLocalHref(href: string | undefined): boolean {
  if (!href) {
    return false;
  }
  if (href.startsWith("/") || href.startsWith("#") || href.startsWith("ticket://")) {
    return true;
  }
  try {
    const base = typeof window !== "undefined" ? window.location.origin : "http://localhost";
    const parsed = new URL(href, base);
    return typeof window !== "undefined" && parsed.origin === window.location.origin;
  } catch {
    return false;
  }
}

const inlineLatexSymbols: Array<[RegExp, string]> = [
  [/\$\s*\\+rightarrow\s*\$/g, "→"],
  [/\$\s*\\+to\s*\$/g, "→"],
  [/\$\s*\\+leftarrow\s*\$/g, "←"],
  [/\$\s*\\+leftrightarrow\s*\$/g, "↔"],
  [/\$\s*\\+Rightarrow\s*\$/g, "⇒"],
  [/\$\s*\\+Leftarrow\s*\$/g, "⇐"],
  [/\$\s*\\+times\s*\$/g, "×"],
  [/\$\s*\\+leq?\s*\$/g, "≤"],
  [/\$\s*\\+geq?\s*\$/g, "≥"]
];

function normalizeMarkdown(content: string) {
  return inlineLatexSymbols.reduce(
    (normalized, [pattern, replacement]) => normalized.replace(pattern, replacement),
    content
  );
}

export function MarkdownContent({
  content,
  className,
  onTicketSelect,
}: {
  content: string;
  className?: string;
  onTicketSelect?: (id: number) => void;
}) {
  const normalized = normalizeMarkdown(content);
  const withTicketLinks = onTicketSelect ? injectTicketLinks(normalized) : normalized;
  const components = buildComponents(onTicketSelect);

  return (
    <div className={cx("markdown-content", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {withTicketLinks}
      </ReactMarkdown>
    </div>
  );
}
