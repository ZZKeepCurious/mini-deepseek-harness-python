import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { TerminalConnection } from "../wire/terminal";
import type {
  TerminalEnvironment,
  TerminalFrame,
  TerminalInfo,
  TerminalServices,
} from "../wire/terminal";
import type { StreamHandle } from "../wire/mux";
import { usePreferences } from "../app/usePreferences";
import { normalizeFontFamily } from "../app/uiPreferences";

interface Props {
  sessionId: string;
  initial: TerminalInfo;
  environment: TerminalEnvironment | null;
  services: TerminalServices;
  onNew: () => void;
  onInfo: (info: TerminalInfo) => void;
}

type Phase = "connecting" | "connected" | "disconnected" | "closed" | "error";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function TerminalScreen({
  sessionId,
  initial,
  environment,
  services,
  onNew,
  onInfo,
}: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const connectionRef = useRef<TerminalConnection | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const measureRef = useRef<(() => void) | null>(null);
  const infoRef = useRef(initial);
  const writableRef = useRef(false);
  const { preferences } = usePreferences();
  const [info, setInfo] = useState(initial);
  const [writable, setWritable] = useState(false);
  const [phase, setPhase] = useState<Phase>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    infoRef.current = info;
  }, [info]);

  useEffect(() => {
    const node = hostRef.current;
    if (node === null) return;
    const term = new Terminal({
      cursorBlink: true,
      fontSize: preferences.terminalFontSize,
      fontFamily:
        normalizeFontFamily(preferences.terminalFontFamily) ||
        'ui-monospace, "Cascadia Code", Consolas, monospace',
      scrollback: environment?.scrollback ?? 1000,
      minimumContrastRatio: 4.5,
      theme: {
        background: "#0b0f18",
        foreground: "#e5e9f0",
        cursor: "#4f8cff",
        selectionBackground: "#3457a8",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(node);
    term.textarea?.setAttribute("aria-label", `终端 ${initial.title}`);
    setPhase("connecting");
    setError(null);

    let disposed = false;
    let retain: StreamHandle | null = null;
    let connection: TerminalConnection | null = null;

    const applyFrame = (frame: TerminalFrame): void => {
      if (frame.type === "snapshot") {
        term.reset();
        term.resize(frame.info.cols, frame.info.rows);
        term.write(frame.screen);
        infoRef.current = frame.info;
        writableRef.current = frame.writable;
        setInfo(frame.info);
        setWritable(frame.writable);
        setPhase(frame.info.state === "running" ? "connected" : "closed");
        onInfo(frame.info);
      } else if (frame.type === "output") {
        term.write(frame.data);
      } else if (frame.type === "state") {
        infoRef.current = frame.info;
        writableRef.current = frame.writable;
        setInfo(frame.info);
        setWritable(frame.writable);
        setPhase(frame.info.state === "running" ? "connected" : "closed");
        onInfo(frame.info);
      }
    };

    void (async () => {
      try {
        retain = services.retain(sessionId, initial.id);
        const first = await retain.next();
        if (disposed) return;
        if (first.type === "error") {
          throw new Error(
            `${first.error.code ?? "gateway/internal"}: ${first.error.message ?? "terminal is unavailable"}`,
          );
        }
        if (
          first.type !== "item" ||
          (first.value as { type?: string } | null)?.type !== "retained"
        ) {
          throw new Error("terminal hold was not acknowledged");
        }
        const handle = services.attach(sessionId, initial.id);
        if (disposed) {
          handle.close();
          return;
        }
        connection = new TerminalConnection(handle, {
          onFrame: applyFrame,
          onWritable: (next) => {
            writableRef.current = next;
            term.options.disableStdin = !next;
            setWritable(next);
            if (next) term.focus();
          },
          onEnd: (reason, failure) => {
            if (disposed || reason === "closed") return;
            writableRef.current = false;
            setWritable(false);
            if (reason === "error") {
              setPhase("error");
              setError(failure?.message ?? "终端连接失败");
            } else {
              setPhase(infoRef.current.state === "running" ? "disconnected" : "closed");
            }
          },
          onIssue: (issue) => {
            setError(issue === "inputFull" ? "终端输入超过缓冲上限" : "终端输入序列失败");
          },
        });
        connectionRef.current = connection;
        connection.setLimits(
          environment?.maxInputBytes ?? Number.POSITIVE_INFINITY,
          environment?.maxBufferedBytes ?? Number.POSITIVE_INFINITY,
        );
      } catch (failure) {
        if (!disposed) {
          setPhase("error");
          setError(errorText(failure));
        }
      }
    })();

    const input = term.onData((data) => connectionRef.current?.write(data));
    const measure = (): void => {
      if (!writableRef.current || node.clientWidth === 0 || node.clientHeight === 0) return;
      const dimensions = fit.proposeDimensions();
      if (!dimensions || !environment) return;
      const cols = Math.min(dimensions.cols, environment.maxCols);
      const rows = Math.min(dimensions.rows, environment.maxRows);
      if (cols < 2 || rows < 1 || (cols === term.cols && rows === term.rows)) return;
      term.resize(cols, rows);
      connectionRef.current?.resize(cols, rows);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    term.focus();
    termRef.current = term;
    measureRef.current = measure;

    return () => {
      disposed = true;
      observer.disconnect();
      input.dispose();
      connection?.close();
      retain?.close();
      connectionRef.current = null;
      termRef.current = null;
      measureRef.current = null;
      term.dispose();
    };
  }, [sessionId, initial.id, environment, services, attempt]);

  // Typography rows in Settings apply to a live terminal: update the cell
  // metrics in place, then re-measure so the PTY size follows.
  useEffect(() => {
    const term = termRef.current;
    if (term === null) return;
    term.options.fontSize = preferences.terminalFontSize;
    term.options.fontFamily =
      normalizeFontFamily(preferences.terminalFontFamily) ||
      'ui-monospace, "Cascadia Code", Consolas, monospace';
    measureRef.current?.();
  }, [preferences.terminalFontSize, preferences.terminalFontFamily]);

  const reconnect = (): void => {
    setAttempt((n) => n + 1);
  };

  const readOnly = phase === "connected" && info.state === "running" && !writable;
  const ended = info.state === "exited" || info.state === "failed" || phase === "closed";
  const retry = !ended && (phase === "disconnected" || phase === "error");

  return (
    <div className="terminal-view">
      {(phase !== "connected" || readOnly || ended || error !== null) && (
        <div className="terminal-status" role="status">
          {phase === "connecting" && "连接中…"}
          {phase === "disconnected" && "连接已断开"}
          {phase === "error" && "终端连接失败"}
          {phase === "closed" && info.state === "exited" && `已退出 (${info.exitCode ?? "—"})`}
          {phase === "closed" && info.state === "failed" && `启动失败${info.error ? `: ${info.error}` : ""}`}
          {phase === "connected" && readOnly && (
            <>
              只读
              <button type="button" className="btn small ghost" onClick={reconnect}>
                接管
              </button>
            </>
          )}
          {retry && (
            <button type="button" className="btn small ghost" onClick={reconnect}>
              重连
            </button>
          )}
          {ended && (
            <button type="button" className="btn small ghost" onClick={onNew}>
              新建终端
            </button>
          )}
        </div>
      )}
      {error !== null && <div className="terminal-error" role="alert">{error}</div>}
      <div className="terminal-host" ref={hostRef} aria-label={`终端 ${info.title}`} />
    </div>
  );
}
