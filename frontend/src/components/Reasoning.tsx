import { Brain, ChevronDown } from "lucide-react";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { cx } from "../lib";

interface ReasoningProps {
  children: ReactNode;
  className?: string;
  isStreaming?: boolean;
}

interface ReasoningContextValue {
  open: boolean;
  setOpen: (next: boolean) => void;
  isStreaming: boolean;
}

const ReasoningContext = createContext<ReasoningContextValue | null>(null);

export function Reasoning({ children, className, isStreaming = false }: ReasoningProps) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (isStreaming) {
      setOpen(true);
      return;
    }
    setOpen(false);
  }, [isStreaming]);

  const contextValue = useMemo<ReasoningContextValue>(
    () => ({
      open,
      setOpen,
      isStreaming,
    }),
    [open, isStreaming],
  );

  return (
    <ReasoningContext.Provider value={contextValue}>
      <section
        className={cx(
          "reasoning",
          className,
          open && "is-open",
          isStreaming && "is-streaming",
        )}
      >
        {children}
      </section>
    </ReasoningContext.Provider>
  );
}

export function ReasoningTrigger() {
  const context = useReasoningContext();
  return (
    <button
      type="button"
      className="reasoning-trigger"
      onClick={() => context.setOpen(!context.open)}
      aria-expanded={context.open}
      aria-label="Toggle assistant reasoning"
    >
      <span className="reasoning-trigger-label">
        <Brain size={14} aria-hidden="true" />
        {context.isStreaming ? "Thinking..." : "Thinking"}
      </span>
      <ChevronDown size={14} aria-hidden="true" className="reasoning-trigger-icon" />
    </button>
  );
}

export function ReasoningContent({ children }: { children: ReactNode }) {
  const context = useReasoningContext();
  if (!context.open) {
    return null;
  }
  return <div className="reasoning-content">{children}</div>;
}

function useReasoningContext(): ReasoningContextValue {
  const context = useContext(ReasoningContext);
  if (!context) {
    throw new Error("Reasoning components must be used inside <Reasoning>.");
  }
  return context;
}
