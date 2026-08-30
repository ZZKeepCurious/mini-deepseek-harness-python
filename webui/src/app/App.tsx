import { useState } from "react";
import { useBackend } from "./useBackend";
import { SessionList } from "../ui/SessionList";
import { Trajectory } from "../ui/Trajectory";
import { ApprovalPanel } from "../ui/ApprovalPanel";
import { ControlPanel } from "../ui/ControlPanel";

export function App() {
  const bk = useBackend();
  const [prompt, setPrompt] = useState("");

  const queue = bk.selectedId ? bk.queues[bk.selectedId] ?? [] : [];
  const jobs = bk.selectedId ? bk.jobs[bk.selectedId] ?? [] : [];

  const submit = () => {
    const text = prompt.trim();
    if (!text) return;
    bk.sendPrompt(text);
    setPrompt("");
  };

  return (
    <div className="shell">
      <SessionList
        sessions={bk.sessions}
        selectedId={bk.selectedId}
        onSelect={bk.selectSession}
        onCreate={bk.createSession}
        creating={false}
      />

      <div className="panel center">
        <div className="panel-header">
          对话
          <span className="spacer" />
          {bk.selectedId && (
            <>
              <span className="dim" style={{ fontFamily: "var(--mono)", fontSize: 11 }}>
                {bk.selectedId}
              </span>
              <button className="btn small ghost" onClick={() => bk.refresh()}>
                刷新
              </button>
            </>
          )}
        </div>
        <div className="panel-body">
          {bk.trajectory.length === 0 && !bk.selectedId && (
            <div className="empty">选择或新建一个会话开始</div>
          )}
          <Trajectory events={bk.trajectory} running={bk.running} />
        </div>
        <div style={{ padding: "8px 12px", borderTop: "1px solid var(--border)", display: "flex", gap: 8 }}>
          <input
            className="input"
            placeholder={
              bk.selectedId ? "输入 prompt 并回车…" : "先选择或新建会话"
            }
            value={prompt}
            disabled={!bk.selectedId}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
            }}
          />
          <button className="btn" onClick={submit} disabled={!bk.selectedId}>
            发送
          </button>
        </div>
      </div>

      <div className="panel">
        <ApprovalPanel
          approvals={bk.approvals}
          sessionId={bk.selectedId}
          onResolve={(a, allowed) => bk.askApproval(a, allowed, bk.selectedId ?? "")}
        />
        <ControlPanel
          queue={queue}
          jobs={jobs}
          running={bk.running}
          sessionId={bk.selectedId}
        />
      </div>

      <div className="statusbar" style={{ gridColumn: "1 / -1" }}>
        {bk.ready && bk.host ? (
          <span>client {bk.host.clientId} · home {bk.host.home}</span>
        ) : (
          <span>connecting…</span>
        )}
        <span>{bk.connected ? "已连接" : "未连接"}</span>
        <span>{bk.sessions.length} 会话</span>
        {bk.error && <span className="err" style={{ color: "var(--err)" }}>{bk.error}</span>}
      </div>
    </div>
  );
}
