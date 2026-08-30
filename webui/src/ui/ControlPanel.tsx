// ControlPanel — right pane: live control for the selected session
// (queue items + jobs) fed by `session.control` replacement frames.

interface Props {
  queue: unknown[];
  jobs: unknown[];
  running: boolean;
  sessionId: string | null;
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
            {queue.map((item, i) => {
              const it = (item ?? {}) as Record<string, unknown>;
              return (
                <div className="row" key={i}>
                  <span>{String(it.name ?? it.toolCallId ?? i)}</span>
                  <span className="dim">{String(it.status ?? "")}</span>
                </div>
              );
            })}
            <div className="panel-header" style={{ fontSize: 12 }}>作业</div>
            {jobs.length === 0 && <div className="empty dim">无作业</div>}
            {jobs.map((job, i) => {
              const j = (job ?? {}) as Record<string, unknown>;
              return (
                <div className="row" key={i}>
                  <span>{String(j.label ?? j.id ?? i)}</span>
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
