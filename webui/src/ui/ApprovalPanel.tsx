// ApprovalPanel — renders pending `approval/request` waterfalls and lets the
// user allow-once or reject via `$events/result` (wire contract).

import type { PendingApproval } from "../app/useBackend";

interface Props {
  approvals: PendingApproval[];
  sessionId: string | null;
  onResolve: (approval: PendingApproval, allowed: boolean) => void;
}

export function ApprovalPanel({ approvals, sessionId, onResolve }: Props) {
  if (approvals.length === 0) return null;
  return (
    <div className="control">
      <div className="panel-header">审批</div>
      <div className="panel-body">
        {approvals.map((a) => (
          <div className="approval" key={a.eventId}>
            <div className="q">
              会话 <span className="tool">{a.agentId}</span> 请求执行工具
              <span className="tool"> {a.toolName || "(未知工具)"}</span>
            </div>
            <div>
              <button className="btn ok small" onClick={() => onResolve(a, true)}>
                允许一次
              </button>{" "}
              <button
                className="btn danger small"
                onClick={() => sessionId && onResolve(a, false)}
                disabled={!sessionId}
              >
                拒绝
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
