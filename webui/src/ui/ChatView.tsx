import { useMemo } from "react";
import type { EventEnvelope } from "../wire";
import { groupRows } from "../trajectory/model";
import { MarkdownText } from "./MarkdownText";
import { ToolCallView } from "./tools/ToolCallView";
import { TurnNotice } from "./TurnNotice";

interface Props {
  events: EventEnvelope[];
  running: boolean;
}

export function ChatView({ events, running }: Props) {
  const rows = useMemo(() => groupRows(events), [events]);

  if (events.length === 0) {
    return <div className="chat-view"><div className="empty">跟随会话中…（等待事件）</div></div>;
  }

  return (
    <div className="chat-view">
      <div className="chat-scroll">
        {rows.map((row) => {
          if (row.kind === "turn") return null;
          if (row.kind === "summary") return null;
          if (row.kind === "notice") {
            return (
              <TurnNotice key={row.key} notice={row.notice} failure={row.failure} />
            );
          }
          if (row.kind === "tool") {
            return (
              <div className="chat-tool" key={row.key}>
                <ToolCallView row={row} terms={[]} />
              </div>
            );
          }
          return (
            <div className={`chat-message ${row.role === "user" ? "user" : "assistant"}`} key={row.key}>
              <div className="chat-who">{row.role === "user" ? "你" : row.role}</div>
              <div className="chat-content">
                {row.blocks.map((block, index) =>
                  row.role === "assistant" ? (
                    <MarkdownText
                      key={index}
                      text={block.text}
                      variant={block.type === "reasoning" ? "compact" : "body"}
                    />
                  ) : (
                    <div key={index} className="chat-user-text">{block.text}</div>
                  ),
                )}
              </div>
            </div>
          );
        })}
        {running && <div className="empty dim">agent 运行中…</div>}
      </div>
    </div>
  );
}
