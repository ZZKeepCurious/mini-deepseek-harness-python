// SessionList — left pane list of sessions + create control.

import type { SessionSummary } from "../wire";

interface Props {
  sessions: SessionSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCreate: (cwd?: string) => void;
  creating: boolean;
}

export function SessionList({
  sessions,
  selectedId,
  onSelect,
  onCreate,
  creating,
}: Props) {
  return (
    <div className="panel">
      <div className="panel-header">
        会话
        <span className="spacer" />
        <button className="btn small" disabled={creating} onClick={() => onCreate(undefined)}>
          新建
        </button>
      </div>
      <div className="panel-body">
        {sessions.length === 0 && (
          <div className="empty">暂无会话。点击「新建」开始。</div>
        )}
        {sessions.map((s) => (
          <div
            key={s.sessionId}
            className={`session-item${s.sessionId === selectedId ? " active" : ""}`}
            onClick={() => onSelect(s.sessionId)}
          >
            <div className="sid">{s.sessionId}</div>
            <div className="meta">
              {s.running ? <span className="badge running">running</span> : <span className="badge idle">idle</span>}
              {" "}{s.cwd ?? ""}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
