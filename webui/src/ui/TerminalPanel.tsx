import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import type {
  TerminalEnvironment,
  TerminalInfo,
  TerminalServices,
  TerminalShell,
} from "../wire/terminal";

const TerminalScreen = lazy(() =>
  import("./TerminalScreen").then((module) => ({ default: module.TerminalScreen })),
);

interface Props {
  sessionId: string | null;
  services: TerminalServices;
}


const SHELL_KEY = "dsh.terminal.shell";

function rememberedShell(): string | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage.getItem(SHELL_KEY);
  } catch {
    return null;
  }
}

function rememberShell(path: string): void {
  try {
    if (typeof localStorage !== "undefined") localStorage.setItem(SHELL_KEY, path);
  } catch {
    return;
  }
}

function terminalId(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  return `term-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function TerminalPanel({ sessionId, services }: Props) {
  const [terminals, setTerminals] = useState<TerminalInfo[]>([]);
  const [shells, setShells] = useState<TerminalShell[]>([]);
  const [environment, setEnvironment] = useState<TerminalEnvironment | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [shellPath, setShellPath] = useState("");
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [editing, setEditing] = useState<{ id: string; title: string } | null>(null);

  useEffect(() => {
    if (!sessionId) {
      setTerminals([]);
      setShells([]);
      setEnvironment(null);
      setActiveId(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setTerminals([]);
    setActiveId(null);
    void Promise.all([services.environment(sessionId), services.list(sessionId)])
      .then(([env, list]) => {
        if (cancelled) return;
        setEnvironment(env);
        setTerminals(list);
        setActiveId(list[0]?.id ?? null);
      })
      .catch((failure: unknown) => {
        if (!cancelled) setError(errorText(failure));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    void services
      .shells(sessionId)
      .then((available) => {
        if (cancelled) return;
        setShells(available);
        const saved = rememberedShell();
        setShellPath(
          available.find((shell) => shell.path === saved)?.path ?? available[0]?.path ?? "",
        );
      })
      .catch(() => {
        if (!cancelled) setShells([]);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, services, loadAttempt]);

  const createTerminal = useCallback(
    async (path = shellPath) => {
      if (!sessionId || !environment || creating) return;
      setCreating(true);
      setError(null);
      try {
        const info = await services.create(sessionId, {
          id: terminalId(),
          cols: Math.min(80, environment.maxCols),
          rows: Math.min(24, environment.maxRows),
          ...(path === "" ? {} : { shellPath: path }),
        });
        rememberShell(info.shell.path);
        setTerminals((current) => [...current, info]);
        setActiveId(info.id);
      } catch (failure) {
        setError(errorText(failure));
      } finally {
        setCreating(false);
      }
    },
    [sessionId, environment, creating, shellPath, services],
  );

  const closeTerminal = useCallback(
    async (id: string) => {
      if (!sessionId) return;
      try {
        await services.close(sessionId, id);
      } catch (failure) {
        setError(errorText(failure));
        return;
      }
      const index = terminals.findIndex((item) => item.id === id);
      const next = terminals.filter((item) => item.id !== id);
      setTerminals(next);
      if (activeId === id) setActiveId(next[Math.max(0, index - 1)]?.id ?? next[0]?.id ?? null);
    },
    [sessionId, services, activeId, terminals],
  );

  const commitRename = useCallback(async () => {
    if (!sessionId || editing === null) return;
    const title = editing.title.trim();
    if (title === "") {
      setEditing(null);
      return;
    }
    try {
      await services.rename(sessionId, editing.id, title);
      setTerminals((current) =>
        current.map((item) => (item.id === editing.id ? { ...item, title } : item)),
      );
      setEditing(null);
    } catch (failure) {
      setError(errorText(failure));
    }
  }, [sessionId, services, editing]);

  const active = terminals.find((item) => item.id === activeId) ?? null;

  return (
    <div className="panel terminal-panel">
      <div className="terminal-toolbar">
        <div className="terminal-tabs" role="tablist" aria-label="终端">
          {terminals.map((item) => (
            <div
              className={`terminal-tab${item.id === activeId ? " active" : ""}${
                item.state !== "running" ? " ended" : ""
              }`}
              key={item.id}
            >
              {editing?.id === item.id ? (
                <input
                  className="terminal-title-input"
                  aria-label="终端名称"
                  autoFocus
                  value={editing.title}
                  onChange={(event) => setEditing({ id: item.id, title: event.target.value })}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void commitRename();
                    if (event.key === "Escape") setEditing(null);
                  }}
                />
              ) : (
                <button
                  type="button"
                  role="tab"
                  aria-selected={item.id === activeId}
                  className="terminal-tab-name"
                  title={item.title}
                  onClick={() => setActiveId(item.id)}
                  onDoubleClick={() => setEditing({ id: item.id, title: item.title })}
                >
                  {item.title}
                </button>
              )}
              <button
                type="button"
                className="terminal-tab-close"
                aria-label={`关闭终端 ${item.title}`}
                onClick={() => void closeTerminal(item.id)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
        <span className="spacer" />
        {shells.length > 0 && (
          <select
            className="terminal-shell"
            aria-label="终端 shell"
            value={shellPath}
            onChange={(event) => {
              setShellPath(event.target.value);
              rememberShell(event.target.value);
            }}
          >
            {shells.map((shell) => (
              <option key={shell.path} value={shell.path}>
                {shell.name}
              </option>
            ))}
          </select>
        )}
        <button
          type="button"
          className="btn small"
          onClick={() => void createTerminal()}
          disabled={loading || creating || !environment}
        >
          {creating ? "创建中…" : "新建"}
        </button>
      </div>

      {error !== null && <div className="terminal-error" role="alert">{error}</div>}

      <div className="terminal-body">
        {!sessionId && <div className="empty">选择会话后打开终端</div>}
        {loading && <div className="empty">加载终端…</div>}
        {!loading && sessionId && error !== null && terminals.length === 0 && (
          <div className="empty">
            终端不可用
            <div className="terminal-guide-actions">
              <button type="button" className="btn small" onClick={() => setLoadAttempt((n) => n + 1)}>
                重试
              </button>
            </div>
          </div>
        )}
        {!loading && sessionId && error === null && terminals.length === 0 && (
          <div className="terminal-guide">
            <div className="terminal-guide-title">在会话工作目录打开一个 shell</div>
            <div className="terminal-guide-path">{environment?.cwd ?? ""}</div>
            <div className="terminal-guide-actions">
              {shells.map((shell) => (
                <button
                  type="button"
                  className="btn small ghost"
                  key={shell.path}
                  title={shell.path}
                  onClick={() => {
                    setShellPath(shell.path);
                    rememberShell(shell.path);
                    void createTerminal(shell.path);
                  }}
                >
                  {shell.name}
                </button>
              ))}
              {shells.length === 0 && (
                <button
                  type="button"
                  className="btn small ghost"
                  onClick={() => void createTerminal("")}
                >
                  使用默认 shell
                </button>
              )}
            </div>
          </div>
        )}
        {!loading && active && sessionId && (
          <Suspense fallback={<div className="empty">连接终端…</div>}>
            <TerminalScreen
              key={`${sessionId}:${active.id}`}
              sessionId={sessionId}
              initial={active}
              environment={environment}
              services={services}
              onNew={() => void createTerminal()}
              onInfo={(next) =>
                setTerminals((current) =>
                  current.map((item) => (item.id === next.id ? next : item)),
                )
              }
            />
          </Suspense>
        )}
      </div>
    </div>
  );
}

