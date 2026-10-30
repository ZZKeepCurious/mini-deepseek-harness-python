import { useState } from "react";
import type { ScheduleDeliveryRecord, ScheduleRecord } from "../wire/schedule";
import { formatScheduleInstant, scheduleKindLabel } from "../wire/schedule";

interface Props {
  sessionId: string | null;
  schedules: ScheduleRecord[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onDelete: (id: string) => void;
  onLoadHistory: (scheduleId: string) => Promise<ScheduleDeliveryRecord[]>;
}

export function SchedulePanel({
  sessionId,
  schedules,
  loading,
  error,
  onRefresh,
  onDelete,
  onLoadHistory,
}: Props) {
  const [history, setHistory] = useState<Record<string, ScheduleDeliveryRecord[]>>({});

  return (
    <div className="panel">
      <div className="panel-header">
        日程
        <span className="spacer" />
        <button
          type="button"
          className="btn small ghost"
          aria-label="刷新日程"
          disabled={sessionId === null || loading}
          onClick={onRefresh}
        >
          刷新
        </button>
      </div>
      <div className="panel-body control">
        {!sessionId && <div className="empty">选择左侧会话查看日程</div>}
        {sessionId && error !== null && <div className="empty error">{error}</div>}
        {sessionId && error === null && loading && schedules.length === 0 && (
          <div className="empty dim">加载中…</div>
        )}
        {sessionId && error === null && !loading && schedules.length === 0 && (
          <div className="empty dim">暂无活跃日程</div>
        )}
        {sessionId &&
          schedules.map((record) => {
            const deliveries = history[record.id];
            return (
              <div className="schedule-row" key={record.id}>
                <div className="schedule-main">
                  <div className="schedule-title">{record.title}</div>
                  <div className="schedule-meta dim">
                    <span>{scheduleKindLabel(record.kind)}</span>
                    <span>{formatScheduleInstant(record.scheduledAt)}</span>
                  </div>
                  {record.prompt !== "" && (
                    <div className="schedule-prompt dim">{record.prompt}</div>
                  )}
                  {deliveries !== undefined && deliveries.length > 0 && (
                    <ul className="schedule-history dim">
                      {deliveries.map((entry) => (
                        <li key={entry.id}>
                          {formatScheduleInstant(entry.deliveredAt)}
                          {entry.messageId !== undefined ? ` · ${entry.messageId}` : ""}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <span className="schedule-actions">
                  <button
                    type="button"
                    className="btn small ghost"
                    aria-label={`日程历史 ${record.title}`}
                    onClick={() => {
                      void onLoadHistory(record.id).then((items) => {
                        setHistory((current) => ({ ...current, [record.id]: items }));
                      });
                    }}
                  >
                    历史
                  </button>
                  <button
                    type="button"
                    className="btn small danger"
                    aria-label={`删除日程 ${record.title}`}
                    onClick={() => onDelete(record.id)}
                  >
                    删除
                  </button>
                </span>
              </div>
            );
          })}
      </div>
    </div>
  );
}
