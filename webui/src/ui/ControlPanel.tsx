import { useState } from "react";
import type { JobView } from "../wire";
import { isJobKillable } from "../wire/jobs";

interface Props {
  queue: unknown[];
  jobs: unknown[];
  running: boolean;
  sessionId: string | null;
  onQueueEdit: (itemId: string, text: string) => void;
  onQueueRemove: (itemId: string) => void;
  onQueueSteer: (itemId: string) => void;
  onJobKill: (jobId: string) => void;
  onJobPreview?: (jobId: string) => Promise<string | null>;
}

function messageText(item: unknown): string {
  const it = (item ?? {}) as Record<string, unknown>;
  const content = Array.isArray(it.content) ? (it.content as unknown[]) : [];
  for (const block of content) {
    const b = (block ?? {}) as Record<string, unknown>;
    if (b.type === "text" && typeof b.text === "string") return b.text;
  }
  return String(it.id ?? "");
}

function messageId(item: unknown): string | null {
  const it = (item ?? {}) as Record<string, unknown>;
  return typeof it.id === "string" && it.id !== "" ? it.id : null;
}

export function ControlPanel({
  queue,
  jobs,
  running,
  sessionId,
  onQueueEdit,
  onQueueRemove,
  onQueueSteer,
  onJobKill,
  onJobPreview,
}: Props) {
  const [editing, setEditing] = useState<{ itemId: string; text: string } | null>(null);
  const [jobPreview, setJobPreview] = useState<Record<string, string>>({});

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
              const itemId = messageId(item);
              const text = messageText(item);
              const isEditing = itemId !== null && editing?.itemId === itemId;
              return (
                <div className="queue-row" key={itemId ?? i}>
                  {isEditing ? (
                    <div className="queue-editor">
                      <textarea
                        className="input"
                        aria-label="编辑队列消息"
                        rows={2}
                        value={editing.text}
                        onChange={(event) => setEditing({ itemId: editing.itemId, text: event.target.value })}
                      />
                      <div className="queue-actions">
                        <button
                          type="button"
                          className="btn small"
                          onClick={() => {
                            const next = editing.text.trim();
                            if (next === "") return;
                            onQueueEdit(editing.itemId, next);
                            setEditing(null);
                          }}
                        >
                          保存
                        </button>
                        <button type="button" className="btn small ghost" onClick={() => setEditing(null)}>
                          取消
                        </button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <span className="queue-text">{text}</span>
                      {itemId !== null && (
                        <span className="queue-actions">
                          <button
                            type="button"
                            className="btn small ghost"
                            onClick={() => setEditing({ itemId, text })}
                          >
                            编辑
                          </button>
                          <button
                            type="button"
                            className="btn small ghost"
                            onClick={() => onQueueSteer(itemId)}
                          >
                            插话
                          </button>
                          <button
                            type="button"
                            className="btn small danger"
                            onClick={() => onQueueRemove(itemId)}
                          >
                            删除
                          </button>
                        </span>
                      )}
                    </>
                  )}
                </div>
              );
            })}
            <div className="panel-header" style={{ fontSize: 12 }}>作业</div>
            {jobs.length === 0 && <div className="empty dim">无作业</div>}
            {jobs.map((job, i) => {
              const j = (job ?? {}) as Partial<JobView>;
              const jobId = typeof j.id === "string" && j.id !== "" ? j.id : null;
              const killable = isJobKillable(j.status);
              const status = String(j.status ?? "");
              const detail =
                typeof j.progress === "string" && j.progress !== ""
                  ? j.progress
                  : typeof j.detail === "string" && j.detail !== ""
                    ? j.detail
                    : null;
              const preview = jobId !== null ? jobPreview[jobId] : undefined;
              return (
                <div className="job-row-block" key={jobId ?? i}>
                  <div className="row job-row">
                    <span className="job-label">{j.label ?? jobId ?? i}</span>
                    <span className="job-meta">
                      {detail !== null && <span className="dim job-detail">{detail}</span>}
                      <span className={`badge ${status || "idle"}`}>{status}</span>
                      {jobId !== null && onJobPreview !== undefined && (
                        <button
                          type="button"
                          className="btn small ghost"
                          aria-label={`预览作业输出 ${j.label ?? jobId}`}
                          onClick={() => {
                            void onJobPreview(jobId).then((text) => {
                              if (text === null) return;
                              setJobPreview((current) => ({ ...current, [jobId]: text }));
                            });
                          }}
                        >
                          输出
                        </button>
                      )}
                      {jobId !== null && killable && (
                        <button
                          type="button"
                          className="btn small danger"
                          aria-label={`终止作业 ${j.label ?? jobId}`}
                          onClick={() => onJobKill(jobId)}
                        >
                          终止
                        </button>
                      )}
                    </span>
                  </div>
                  {preview !== undefined && (
                    <pre className="job-preview dim">{preview}</pre>
                  )}
                </div>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}
