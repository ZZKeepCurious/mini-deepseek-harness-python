import { useState } from "react";
import type { ScheduleCatalogEntry, ScheduleDeliveryRecord, ScheduleRecord } from "../wire/schedule";
import { formatScheduleInstant, scheduleKindLabel } from "../wire/schedule";

type PanelScope = "session" | "catalog";

interface Props {
  sessionId: string | null;
  schedules: ScheduleRecord[];
  catalog: ScheduleCatalogEntry[];
  loading: boolean;
  catalogLoading: boolean;
  error: string | null;
  catalogError: string | null;
  onRefresh: () => void;
  onRefreshCatalog: () => void;
  onDelete: (id: string, sessionIdOverride?: string) => void;
  onUpdate: (
    id: string,
    expected: ScheduleRecord,
    patch: { title?: string; prompt?: string },
    sessionIdOverride?: string,
  ) => void;
  onLoadHistory: (scheduleId: string, sessionIdOverride?: string) => Promise<ScheduleDeliveryRecord[]>;
}

export function SchedulePanel({
  sessionId,
  schedules,
  catalog,
  loading,
  catalogLoading,
  error,
  catalogError,
  onRefresh,
  onRefreshCatalog,
  onDelete,
  onUpdate,
  onLoadHistory,
}: Props) {
  const [scope, setScope] = useState<PanelScope>("session");
  const [history, setHistory] = useState<Record<string, ScheduleDeliveryRecord[]>>({});
  const [editing, setEditing] = useState<{
    id: string;
    sessionId: string;
    title: string;
    prompt: string;
    expected: ScheduleRecord;
  } | null>(null);

  const activeError = scope === "session" ? error : catalogError;
  const activeLoading = scope === "session" ? loading : catalogLoading;
  const rows: Array<ScheduleRecord & { sessionId?: string; status?: string }> =
    scope === "session" ? schedules : catalog.filter((entry) => entry.status === "active");

  return (
    <div className="panel">
      <div className="panel-header">
        日程
        <span className="spacer" />
        <button
          type="button"
          className={`btn small ghost${scope === "session" ? " active" : ""}`}
          aria-pressed={scope === "session"}
          onClick={() => setScope("session")}
        >
          本会话
        </button>
        <button
          type="button"
          className={`btn small ghost${scope === "catalog" ? " active" : ""}`}
          aria-pressed={scope === "catalog"}
          onClick={() => setScope("catalog")}
        >
          全部任务
        </button>
        <button
          type="button"
          className="btn small ghost"
          aria-label="刷新日程"
          disabled={scope === "session" ? sessionId === null || loading : catalogLoading}
          onClick={() => (scope === "session" ? onRefresh() : onRefreshCatalog())}
        >
          刷新
        </button>
      </div>
      <div className="panel-body control">
        {scope === "session" && !sessionId && (
          <div className="empty">选择左侧会话查看日程</div>
        )}
        {activeError !== null && <div className="empty error">{activeError}</div>}
        {((scope === "session" && sessionId) || scope === "catalog") &&
          activeError === null &&
          activeLoading &&
          rows.length === 0 && <div className="empty dim">加载中…</div>}
        {((scope === "session" && sessionId) || scope === "catalog") &&
          activeError === null &&
          !activeLoading &&
          rows.length === 0 && <div className="empty dim">暂无活跃日程</div>}
        {rows.map((record) => {
          const ownerSession = record.sessionId ?? sessionId ?? "";
          const historyKey = `${ownerSession}:${record.id}`;
          const deliveries = history[historyKey];
          const isEditing = editing?.id === record.id && editing.sessionId === ownerSession;
          return (
            <div className="schedule-row" key={historyKey}>
              <div className="schedule-main">
                {isEditing ? (
                  <>
                    <label className="schedule-edit-field">
                      标题
                      <input
                        className="input"
                        value={editing.title}
                        onChange={(event) =>
                          setEditing({ ...editing, title: event.target.value })
                        }
                      />
                    </label>
                    <label className="schedule-edit-field">
                      指令
                      <textarea
                        className="input"
                        rows={3}
                        value={editing.prompt}
                        onChange={(event) =>
                          setEditing({ ...editing, prompt: event.target.value })
                        }
                      />
                    </label>
                  </>
                ) : (
                  <>
                    <div className="schedule-title">{record.title}</div>
                    <div className="schedule-meta dim">
                      {scope === "catalog" && record.sessionId !== undefined && (
                        <span>{record.sessionId}</span>
                      )}
                      <span>{scheduleKindLabel(record.kind)}</span>
                      <span>{formatScheduleInstant(record.scheduledAt)}</span>
                    </div>
                    {record.prompt !== "" && (
                      <div className="schedule-prompt dim">{record.prompt}</div>
                    )}
                  </>
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
                {isEditing ? (
                  <>
                    <button
                      type="button"
                      className="btn small"
                      aria-label={`保存日程 ${record.title}`}
                      onClick={() => {
                        onUpdate(
                          record.id,
                          editing.expected,
                          { title: editing.title, prompt: editing.prompt },
                          ownerSession,
                        );
                        setEditing(null);
                      }}
                    >
                      保存
                    </button>
                    <button
                      type="button"
                      className="btn small ghost"
                      aria-label="取消编辑"
                      onClick={() => setEditing(null)}
                    >
                      取消
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      className="btn small ghost"
                      aria-label={`编辑日程 ${record.title}`}
                      disabled={ownerSession === ""}
                      onClick={() =>
                        setEditing({
                          id: record.id,
                          sessionId: ownerSession,
                          title: record.title,
                          prompt: record.prompt,
                          expected: {
                            id: record.id,
                            kind: record.kind,
                            title: record.title,
                            prompt: record.prompt,
                            scheduledAt: record.scheduledAt,
                          },
                        })
                      }
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      className="btn small ghost"
                      aria-label={`日程历史 ${record.title}`}
                      disabled={ownerSession === ""}
                      onClick={() => {
                        void onLoadHistory(record.id, ownerSession).then((items) => {
                          setHistory((current) => ({ ...current, [historyKey]: items }));
                        });
                      }}
                    >
                      历史
                    </button>
                    <button
                      type="button"
                      className="btn small danger"
                      aria-label={`删除日程 ${record.title}`}
                      disabled={ownerSession === ""}
                      onClick={() => onDelete(record.id, ownerSession)}
                    >
                      删除
                    </button>
                  </>
                )}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
