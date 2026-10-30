// SessionList — left pane list of sessions + create control.
// Tilde paths, hover actions (fork/archive), create refusal, archived filter.

import { useMemo, useState } from "react";
import type { SessionSummary } from "../wire";
import { displayHomePath } from "../wire/home-path";
import { groupSessionsByWorkspace, type WorkspaceRoster } from "../wire";

interface Props {
  sessions: SessionSummary[];
  selectedId: string | null;
  /** Sessions whose pending approval/request waits for a decision. */
  pending?: ReadonlySet<string>;
  home?: string;
  archivedIds?: readonly string[];
  workspaceRoster?: WorkspaceRoster;
  showArchived?: boolean;
  createError?: string | null;
  onSelect: (id: string) => void;
  onCreate: (cwd?: string) => void;
  onFork?: (id: string) => void;
  onArchive?: (id: string) => void;
  onUnarchive?: (id: string) => void;
  onShowArchived?: (show: boolean) => void;
  creating: boolean;
}

export function SessionList({
  sessions,
  selectedId,
  pending,
  home,
  archivedIds = [],
  workspaceRoster,
  showArchived = false,
  createError,
  onSelect,
  onCreate,
  onFork,
  onArchive,
  onUnarchive,
  onShowArchived,
  creating,
}: Props) {
  const archived = new Set(archivedIds);
  const visible = sessions.filter((session) => showArchived || !archived.has(session.sessionId));
  const visibleIds = useMemo(() => new Set(visible.map((s) => s.sessionId)), [visible]);
  const groups = useMemo(
    () =>
      workspaceRoster === undefined
        ? [{ workspaceId: null as string | null, title: "全部会话", sessions: visible }]
        : groupSessionsByWorkspace(workspaceRoster, sessions, visibleIds),
    [workspaceRoster, sessions, visible, visibleIds],
  );
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());

  const renderSession = (session: SessionSummary) => {
    const waiting = pending?.has(session.sessionId) ?? false;
    const isArchived = archived.has(session.sessionId);
    const path = displayHomePath(home, session.cwd);
    return (
      <div
        key={session.sessionId}
        className={`session-item${session.sessionId === selectedId ? " active" : ""}`}
        onClick={() => onSelect(session.sessionId)}
      >
        <div className="sid">{session.sessionId}</div>
        <div className="meta">
          {waiting ? (
            <span className="badge attention">待审批</span>
          ) : isArchived ? (
            <span className="badge">已归档</span>
          ) : session.running ? (
            <span className="badge running">running</span>
          ) : (
            <span className="badge idle">idle</span>
          )}
          {" "}
          {path !== "" && (
            <span className="session-path" title={session.cwd ?? path}>{path}</span>
          )}
        </div>
        <div className="session-actions">
          {isArchived ? (
            <button
              type="button"
              className="btn small ghost"
              aria-label="取消归档"
              onClick={(event) => {
                event.stopPropagation();
                onUnarchive?.(session.sessionId);
              }}
            >
              取消归档
            </button>
          ) : (
            <>
              <button
                type="button"
                className="btn small ghost"
                aria-label="分叉会话"
                onClick={(event) => {
                  event.stopPropagation();
                  onFork?.(session.sessionId);
                }}
              >
                分叉
              </button>
              <button
                type="button"
                className="btn small ghost"
                aria-label="归档会话"
                onClick={(event) => {
                  event.stopPropagation();
                  onArchive?.(session.sessionId);
                }}
              >
                归档
              </button>
            </>
          )}
        </div>
      </div>
    );
  };

  return (
    <div className="panel">
      <div className="panel-header">
        会话
        <span className="spacer" />
        {archived.size > 0 && (
          <button
            type="button"
            className="btn small ghost"
            onClick={() => { onShowArchived?.(!showArchived); }}
          >
            {showArchived ? "隐藏已归档" : "已归档"}
          </button>
        )}
        <button className="btn small" disabled={creating} onClick={() => onCreate(undefined)}>
          新建
        </button>
      </div>
      <div className="panel-body">
        {createError ? (
          <div className="session-create-error" role="alert">
            <div>{createError}</div>
            <button type="button" className="btn small" onClick={() => onCreate(undefined)}>
              重试新建
            </button>
          </div>
        ) : null}
        {visible.length === 0 && (
          <div className="empty">暂无会话。点击「新建」开始。</div>
        )}
        {groups.map((group) => {
          const key = group.workspaceId ?? "ungrouped";
          const folded = collapsed.has(key);
          return (
            <div className="workspace-group" key={key}>
              {groups.length > 1 && (
                <button
                  type="button"
                  className="workspace-group-header"
                  aria-expanded={!folded}
                  onClick={() => {
                    setCollapsed((current) => {
                      const next = new Set(current);
                      if (next.has(key)) next.delete(key);
                      else next.add(key);
                      return next;
                    });
                  }}
                >
                  <span>{group.title}</span>
                  <span className="dim">{group.sessions.length}</span>
                </button>
              )}
              {!folded && group.sessions.map((session) => renderSession(session))}
            </div>
          );
        })}
      </div>
    </div>
  );
}
