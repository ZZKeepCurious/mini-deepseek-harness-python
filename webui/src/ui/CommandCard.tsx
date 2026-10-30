import { useState } from "react";
import type { CommandRow } from "../trajectory/model";

export interface CommandCardProps {
  row: CommandRow;
}

function compactText(text: string): string {
  return text.split(/\s+/).join(" ").trim();
}

export function CommandCard({ row }: CommandCardProps) {
  const [expanded, setExpanded] = useState(false);
  const text = row.outcome?.text;
  const summary = text === undefined
    ? (row.outcome === null ? "运行中" : row.outcome.kind === "error" ? "失败" : "完成")
    : compactText(text);
  const body = text !== undefined && text.includes("\n") ? text : null;
  const open = expanded && body !== null;
  const state = row.outcome === null ? "running" : row.outcome.kind === "error" ? "error" : "ok";

  return (
    <div className="command-card" data-state={state}>
      <button
        type="button"
        className="command-card-row"
        disabled={body === null}
        onClick={() => { if (body !== null) setExpanded((value) => !value); }}
      >
        <span className="command-card-name">{row.name}</span>
        <span className="command-card-summary" data-error={state === "error" || undefined}>{summary}</span>
      </button>
      {open && <pre className="command-card-body" data-error={state === "error" || undefined}>{body}</pre>}
    </div>
  );
}
