// ControlPanel — right pane: live control for the selected session.
// Queue = inbox projection (pending next-turn/next-step user messages); jobs =
// job-controller `job/list` roster rows. Both come from the rc.1 wire
// (projection frames + job/list stream), superseding the retired queue/jobs
// control frames.

import type { JobView } from "../wire";

interface Props {
  queue: unknown[];
  jobs: unknown[];
  running: boolean;
  sessionId: string | null;
}

/** Extract the first text block of a pending user message for the row label. */
function messageText(item: unknown): string {
  const it = (item ?? {}) as Record<string, unknown>;
  const content = Array.isArray(it.content) ? (it.content as unknown[]) : [];
  for (const block of content) {
    const b = (block ?? {}) as Record<string, unknown>;
    if (b.type === "text" && typeof b.text === "string") return b.text;
  }
  return String(it.id ?? "");
}

export function ControlPanel({ queue, jobs, running, sessionId }: Props) {
  return (
    <div className="panel">
      <div className="panel-header">
        控制台
        <span className="spacer" />
        {running ? <span className="badge running">running</span> : <span className="badge idle">idle</span>}
      </div>
      <div className="panel-body control">
        {!sessionId && <div className="empty">选择左侧会话查看队列 / 作业</div>}
        {sessionId && (
          <>
            <div className="panel-header" style={{ fontSize: 12 }}>队列</div>
            {queue.length === 0 && <div className="empty dim">队列空</div>}
            {queue.map((item, i) => (
              <div className="row" key={i}>
                <span>{messageText(item)}</span>
                <span className="dim">pending</span>
              </div>
            ))}
            <div className="panel-header" style={{ fontSize: 12 }}>作业</div>
            {jobs.length === 0 && <div className="empty dim">无作业</div>}
            {jobs.map((job, i) => {
              const j = (job ?? {}) as Partial<JobView>;
              return (
                <div className="row" key={i}>
                  <span>{j.label ?? j.id ?? i}</span>
                  <span className={`badge ${String(j.status ?? "idle")}`}>
                    {String(j.status ?? "")}
                  </span>
                </div>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}