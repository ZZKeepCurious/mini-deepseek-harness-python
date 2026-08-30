// Trajectory — renders the followed session as turns of events.
// Consumes EventEnvelope[] from the wire trajectory buffer (session.follow).

import type { ReactNode } from "react";
import type { EventEnvelope } from "../wire";

interface Props {
  events: EventEnvelope[];
  running: boolean;
}

export function Trajectory({ events, running }: Props) {
  if (events.length === 0) {
    return <div className="empty">跟随会话中…（等待事件）</div>;
  }
  return (
    <div className="trajectory">
      <TurnList events={events} />
      {running && <div className="empty dim">agent 运行中…</div>}
    </div>
  );
}

function TurnList({ events }: { events: EventEnvelope[] }) {
  // Simple grouping: render message/tool events in seq order, annotating turns
  // when a turn/start marker appears.
  const nodes: ReactNode[] = [];
  let key = 0;
  for (const ev of events) {
    const data = (ev.data ?? {}) as Record<string, unknown>;
    if (ev.type === "event/envelope" || ev.type === "session/append") {
      const msg = data as { message?: { role?: string } };
      const role = msg.message?.role ?? "unknown";
      nodes.push(<MessageBlock key={++key} seq={ev.seq} role={role} data={data} />);
    } else if (ev.type === "turn/start") {
      nodes.push(
        <div key={`t${ev.seq}`} className="turn-head">
          — turn {String(data.turn ?? ev.seq)} —
        </div>
      );
    } else if (ev.type === "tool/call") {
      nodes.push(
        <div key={++key} className="tool-call">
          <span className="label">工具调用:</span> {String(data.name ?? "?")}(
          {typeof data.arguments === "string" ? data.arguments : JSON.stringify(data.arguments)})
        </div>
      );
    } else if (ev.type === "tool/result") {
      const isErr = Boolean(data.isError);
      nodes.push(
        <div key={++key} className={`tool-result${isErr ? " error" : ""}`}>
          <span className="label">{isErr ? "工具错误:" : "工具结果:"}</span>{" "}
          {String(data.content ?? "")}
        </div>
      );
    }
  }
  return <>{nodes}</>;
}

function MessageBlock({ seq, role, data }: { seq: number; role: string; data: Record<string, unknown> }) {
  const msg = data.message as {
    content?: Array<{ type?: string; text?: string }>;
  };
  const content = Array.isArray(msg?.content) ? msg.content : [];
  return (
    <div key={seq} className={`msg ${role === "user" ? "user" : "assistant"}`}>
      <span className="who">{role}</span>
      {content.map((blk, i) => {
        if (blk.type === "reasoning") {
          return (
            <div className="block reasoning" key={i}>
              {blk.text}
            </div>
          );
        }
        return (
          <div className="block" key={i}>
            {blk.text}
          </div>
        );
      })}
    </div>
  );
}
