import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cx } from "../lib";

const markdownComponents: Components = {
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />
};

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

export function MarkdownContent({ content, className }: { content: string; className?: string }) {
  const normalizedContent = normalizeMarkdown(content);

  return (
    <div className={cx("markdown-content", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
        {normalizedContent}
      </ReactMarkdown>
    </div>
  );
}
