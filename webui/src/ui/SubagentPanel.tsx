import type { SessionSummary } from "../wire";
import { shortSessionId } from "../wire/subagents";

interface Props {
  parentSessionId: string;
  childSessions: SessionSummary[];
  onOpen: (sessionId: string) => void;
}

export function SubagentPanel({ parentSessionId, childSessions, onOpen }: Props) {
  if (childSessions.length === 0) return null;

  return (
    <div className="panel subagent-panel" data-parent={parentSessionId}>
      <div className="panel-header" style={{ fontSize: 12 }}>
        子代理
        <span className="spacer" />
        <span className="dim">{childSessions.length}</span>
      </div>
      <div className="panel-body subagent-list">
        {childSessions.map((session) => {
          const label = shortSessionId(session.sessionId);
          const running = session.running === true;
          return (
            <div className="subagent-row" key={session.sessionId}>
              <button
                type="button"
                className="btn small ghost subagent-open"
                aria-label={`打开子代理 ${session.sessionId}`}
                onClick={() => onOpen(session.sessionId)}
              >
                {label}
              </button>
              {running ? (
                <span className="badge running">running</span>
              ) : (
                <span className="badge idle">idle</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
