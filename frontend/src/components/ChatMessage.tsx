import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AgentResponse } from "../types";
import { DynamicChart } from "./DynamicChart";

interface AgentResponseMessageProps {
  message: AgentResponse;
  role?: "user" | "assistant";
}

export function AgentResponseMessage({ message, role = "assistant" }: AgentResponseMessageProps) {
  const isAssistant = role === "assistant";

  return (
    <div className={`chat-message chat-message--${role}`}>
      <div className="chat-message__label">{isAssistant ? "Assistant" : "You"}</div>
      <div className="chat-message__bubble">
        {message.markdown_text && !message.chart && (
          <div className="chat-message__text">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.markdown_text}</ReactMarkdown>
          </div>
        )}
        {message.chart && (
          <div className="chat-message__chart">
            <DynamicChart config={message.chart} />
          </div>
        )}
      </div>
    </div>
  );
}
