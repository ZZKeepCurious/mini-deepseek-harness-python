import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { StreamHandle } from "../wire/mux";
import type {
  WorkspaceChangeFrame,
  WorkspaceFileEntry,
  WorkspaceServices,
} from "../wire/workspace";

interface Props {
  sessionId: string | null;
  services: WorkspaceServices;
  onOpen: (path: string) => void;
}

function childPath(parent: string, name: string): string {
  return parent === "." ? name : `${parent}/${name}`;
}

function entryLabel(entry: WorkspaceFileEntry): string {
  if (entry.type !== "file" || entry.size === undefined) return entry.name;
  const size = entry.size < 1024 ? `${entry.size} B` : `${Math.round(entry.size / 1024)} KB`;
  return `${entry.name} · ${size}`;
}

export function FilesPanel({ sessionId, services, onOpen }: Props) {
  const [loaded, setLoaded] = useState<Record<string, WorkspaceFileEntry[]>>({});
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const [loading, setLoading] = useState<ReadonlySet<string>>(() => new Set());
  const [error, setError] = useState<string | null>(null);
  const activeReads = useRef<Set<string>>(new Set());
  const queuedReads = useRef<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    setLoaded({});
    setExpanded(new Set());
    setLoading(new Set());
    setError(null);
    activeReads.current.clear();
    queuedReads.current.clear();
    if (sessionId === null) return;
    void services.list(sessionId, ".")
      .then((result) => {
        if (!cancelled) setLoaded({ ".": result.entries });
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [services, sessionId]);

  const loadDirectory = useCallback(async (path: string) => {
    if (sessionId === null) return;
    if (activeReads.current.has(path)) {
      queuedReads.current.add(path);
      return;
    }
    activeReads.current.add(path);
    setLoading((current) => new Set(current).add(path));
    try {
      for (;;) {
        queuedReads.current.delete(path);
        const result = await services.list(sessionId, path);
        setLoaded((current) => ({ ...current, [path]: result.entries }));
        if (!queuedReads.current.delete(path)) break;
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      activeReads.current.delete(path);
      setLoading((current) => {
        const next = new Set(current);
        next.delete(path);
        return next;
      });
    }
  }, [services, sessionId]);

  // One watch per open level. The key keeps a session reset (a fresh empty set)
  // from bouncing every stream when the expansion contents did not change.
  const watchKey = [".", ...expanded].join("\0");

  useEffect(() => {
    if (sessionId === null) return;
    let cancelled = false;
    const handles: StreamHandle[] = [];
    const paths = watchKey.split("\0");
    for (const path of paths) {
      let handle: StreamHandle;
      try {
        handle = services.watch(sessionId, path);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
        continue;
      }
      handles.push(handle);
      void (async () => {
        for (;;) {
          const frame = await handle.next();
          if (cancelled) return;
          if (frame.type !== "item") return;
          const value = frame.value as WorkspaceChangeFrame;
          if (value.kind === "change") await loadDirectory(path);
        }
      })().catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      });
    }
    return () => {
      cancelled = true;
      for (const handle of handles) handle.close();
    };
  }, [services, sessionId, watchKey, loadDirectory]);

  const toggle = (path: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
    if (!(path in loaded) && !loading.has(path)) void loadDirectory(path);
  };

  const renderEntries = (parent: string, depth: number): ReactNode =>
    (loaded[parent] ?? []).map((entry) => {
      const path = childPath(parent, entry.name);
      const directory = entry.type === "directory";
      const isOpen = expanded.has(path);
      const pending = loading.has(path);
      return (
        <div key={path}>
          <button
            type="button"
            role="treeitem"
            aria-expanded={directory ? isOpen : undefined}
            className={`file-row${directory ? " directory" : ""}`}
            style={{ paddingLeft: `${8 + depth * 14}px` }}
            onClick={() => (directory ? toggle(path) : onOpen(path))}
          >
            <span className="file-mark">
              {directory ? (pending ? "…" : isOpen ? "−" : "+") : "·"}
            </span>
            <span className="file-name">{entryLabel(entry)}</span>
          </button>
          {directory && isOpen && renderEntries(path, depth + 1)}
        </div>
      );
    });

  return (
    <div className="panel files-panel">
      <div className="panel-header">
        文件
        <span className="spacer" />
        <span className="dim" style={{ fontSize: 11 }}>{sessionId ?? "未选择会话"}</span>
      </div>
      <div className="panel-body">
        {sessionId === null && <div className="empty">选择会话查看工作区文件</div>}
        {error !== null && <div className="file-error" role="alert">{error}</div>}
        {sessionId !== null && loaded["."] === undefined && error === null && (
          <div className="empty dim">加载中…</div>
        )}
        {sessionId !== null && (
          <div role="tree" aria-label="工作区文件" className="file-tree">
            {renderEntries(".", 0)}
          </div>
        )}
      </div>
    </div>
  );
}