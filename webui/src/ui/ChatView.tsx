import { useMemo } from "react";
import type { EventEnvelope } from "../wire";
import { groupRows } from "../trajectory/model";
import { MarkdownText } from "./MarkdownText";
import { ToolCallView } from "./tools/ToolCallView";
import { TurnNotice } from "./TurnNotice";
import { CommandCard } from "./CommandCard";
import { HistoryPager } from "./HistoryPager";
import { MessageFeedbackButtons } from "./MessageFeedbackButtons";
import type { MessageRating } from "../wire/feedback";

interface Props {
  events: EventEnvelope[];
  running: boolean;
  blankSession?: boolean;
  historyHasMore?: boolean;
  onLoadEarlier?: () => void;
  feedbackEnabled?: boolean;
  feedbackRating?: (messageId: string) => MessageRating | undefined;
  onFeedbackRate?: (messageId: string, rating: MessageRating) => void;
  onFeedbackClear?: (messageId: string) => void;
}

export function ChatView({
  events,
  running,
  blankSession = false,
  historyHasMore = false,
  onLoadEarlier,
  feedbackEnabled = false,
  feedbackRating,
  onFeedbackRate,
  onFeedbackClear,
}: Props) {
  const rows = useMemo(() => groupRows(events), [events]);

  if (events.length === 0) {
    return (
      <div className="chat-view">
        <div className="empty">
          {blankSession ? "空白会话，可直接输入开始对话。" : "跟随会话中…（等待事件）"}
        </div>
      </div>
    );
  }

  return (
    <div className="chat-view">
      <div className="chat-scroll">
        {onLoadEarlier !== undefined && (
          <HistoryPager hasMore={historyHasMore} onLoad={onLoadEarlier} />
        )}
        {rows.map((row) => {
          if (row.kind === "turn") return null;
          if (row.kind === "summary") return null;
          if (row.kind === "notice") {
            return (
              <TurnNotice key={row.key} notice={row.notice} failure={row.failure} />
            );
          }
          if (row.kind === "command") {
            return (
              <div className="chat-command" key={row.key}>
                <CommandCard row={row} />
              </div>
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
              <div className="chat-who">
                {row.role === "user" ? "你" : row.role}
                {feedbackEnabled &&
                  row.role === "assistant" &&
                  row.messageId !== undefined &&
                  onFeedbackRate !== undefined &&
                  onFeedbackClear !== undefined && (
                    <MessageFeedbackButtons
                      messageId={row.messageId}
                      rating={feedbackRating?.(row.messageId)}
                      onRate={onFeedbackRate}
                      onClear={onFeedbackClear}
                    />
                  )}
              </div>
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
