import { rpc } from "./rpc";
import type { StreamHandle } from "./mux";

export interface TerminalShell {
  path: string;
  name: string;
  args: string[];
}

export interface TerminalEnvironment {
  cwd: string;
  maxInputBytes: number;
  maxBufferedBytes: number;
  maxCols: number;
  maxRows: number;
  scrollback: number;
}

export type TerminalState = "running" | "exited" | "failed";

export interface TerminalInfo {
  id: string;
  title: string;
  shell: TerminalShell;
  cwd: string;
  cols: number;
  rows: number;
  state: TerminalState;
  exitCode?: number | null;
  error?: string;
}

export interface TerminalInputWindow {
  maxBytes: number;
  frameOverheadBytes: number;
}

export type TerminalFrame =
  | {
      type: "snapshot";
      sequence: number;
      screen: string;
      info: TerminalInfo;
      writable: boolean;
      inputWindow?: TerminalInputWindow;
    }
  | { type: "output"; sequence: number; data: string }
  | { type: "state"; info: TerminalInfo; writable: boolean }
  | { type: "ack"; sequence: number; refusal?: "read-only" | "not-running" };

export type TerminalInput =
  | { type: "write"; sequence: number; data: string }
  | { type: "resize"; sequence: number; cols: number; rows: number };

export interface TerminalCreateRequest {
  id: string;
  cols: number;
  rows: number;
  shellPath?: string;
}

export interface TerminalMux {
  openStream(endpoint: string, payload: unknown): StreamHandle;
}

export interface TerminalServices {
  environment(sessionId: string): Promise<TerminalEnvironment>;
  shells(sessionId: string): Promise<TerminalShell[]>;
  list(sessionId: string): Promise<TerminalInfo[]>;
  create(sessionId: string, request: TerminalCreateRequest): Promise<TerminalInfo>;
  rename(sessionId: string, id: string, title: string): Promise<void>;
  close(sessionId: string, id: string): Promise<void>;
  retain(sessionId: string, id: string): StreamHandle;
  attach(sessionId: string, id: string): StreamHandle;
}

type Call = <T>(method: string, payload?: unknown) => Promise<T>;

export function createTerminalServices(mux: TerminalMux, call: Call = rpc): TerminalServices {
  return {
    environment: (sessionId) =>
      call<TerminalEnvironment>("terminal/environment", { agentId: sessionId }),
    shells: (sessionId) =>
      call<TerminalShell[]>("terminal/shells", { agentId: sessionId }),
    list: (sessionId) =>
      call<TerminalInfo[]>("terminal/list", { sessionId }),
    create: (sessionId, request) =>
      call<TerminalInfo>("terminal/create", { agentId: sessionId, ...request }),
    rename: (sessionId, id, title) =>
      call<void>("terminal/rename", { agentId: sessionId, id, title }),
    close: (sessionId, id) =>
      call<void>("terminal/close", { agentId: sessionId, id }),
    retain: (sessionId, id) =>
      mux.openStream("terminal/retain", { args: { sessionId, id } }),
    attach: (sessionId, id) =>
      mux.openStream("terminal/attach", { args: { agentId: sessionId, id } }),
  };
}

export type TerminalEndReason = "ended" | "closed" | "error";

export interface TerminalConnectionCallbacks {
  onFrame(frame: TerminalFrame): void;
  onWritable(writable: boolean): void;
  onEnd(reason: TerminalEndReason, error?: Error): void;
  onIssue?(issue: "inputFull" | "protocol"): void;
}

interface QueuedInput {
  write?: string;
  resize?: { cols: number; rows: number };
  inputBytes: number;
  frameBytes: number;
}

export class TerminalConnection {
  private sequence = 0;
  private acknowledged = 0;
  private expected = -1;
  private writable = false;
  private closed = false;
  private finished = false;
  private inFlight = false;
  private queue: QueuedInput[] = [];
  private queuedBytes = 0;
  private inFlightBytes = 0;
  private window: TerminalInputWindow | null = null;
  private maxInputBytes = Number.POSITIVE_INFINITY;
  private maxBufferedBytes = Number.POSITIVE_INFINITY;

  constructor(
    private readonly handle: StreamHandle,
    private readonly callbacks: TerminalConnectionCallbacks,
  ) {
    void this.consume();
  }

  setLimits(maxInputBytes: number, maxBufferedBytes: number): void {
    this.maxInputBytes = maxInputBytes;
    this.maxBufferedBytes = maxBufferedBytes;
  }

  setWritable(writable: boolean): void {
    this.writable = writable;
    if (!writable) this.clearQueue();
    else this.pump();
  }

  write(data: string): void {
    if (!this.writable || this.closed || data.length === 0) return;
    const inputBytes = new TextEncoder().encode(data).byteLength;
    if (inputBytes > this.maxInputBytes) {
      this.fail("inputFull");
      return;
    }
    this.enqueue({ write: data, inputBytes, frameBytes: inputBytes + 32 });
  }

  resize(cols: number, rows: number): void {
    if (!this.writable || this.closed) return;
    this.enqueue({
      resize: { cols, rows },
      inputBytes: 0,
      frameBytes: 48,
    });
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.handle.close();
    this.finish("closed");
  }

  private enqueue(item: QueuedInput): void {
    if (this.window && item.frameBytes > this.window.maxBytes) {
      this.fail("inputFull");
      return;
    }
    if (this.queuedBytes + item.frameBytes > this.maxBufferedBytes) {
      this.fail("inputFull");
      return;
    }
    this.queue.push(item);
    this.queuedBytes += item.frameBytes;
    this.pump();
  }

  private pump(): void {
    if (this.closed || this.inFlight || !this.writable) return;
    const item = this.queue.shift();
    if (!item) return;
    this.queuedBytes -= item.frameBytes;
    const sequence = this.sequence + 1;
    const command: TerminalInput = item.resize
      ? { type: "resize", sequence, ...item.resize }
      : { type: "write", sequence, data: item.write ?? "" };
    const frameBytes =
      new TextEncoder().encode(JSON.stringify(command)).byteLength +
      (this.window?.frameOverheadBytes ?? 0);
    if (this.window && frameBytes > this.window.maxBytes) {
      this.fail("inputFull");
      return;
    }
    if (this.window && this.inFlightBytes + frameBytes > this.window.maxBytes) {
      this.queue.unshift(item);
      this.queuedBytes += item.frameBytes;
      return;
    }
    this.sequence = sequence;
    this.inFlight = true;
    this.inFlightBytes = frameBytes;
    try {
      this.handle.send(command);
    } catch (error) {
      this.fail("protocol", error);
      return;
    }
  }

  private clearQueue(): void {
    this.queue = [];
    this.queuedBytes = 0;
  }

  private async consume(): Promise<void> {
    try {
      for (;;) {
        const frame = await this.handle.next();
        if (this.closed) return;
        if (frame.type === "end") {
          this.finish("ended");
          return;
        }
        if (frame.type === "error") {
          const error = new Error(
            `${frame.error.code ?? "gateway/internal"}: ${frame.error.message ?? "terminal stream failed"}`,
          );
          this.finish("error", error);
          return;
        }
        const value = frame.value as TerminalFrame;
        if (!this.accept(value)) return;
      }
    } catch (error) {
      if (!this.closed) this.finish("error", error instanceof Error ? error : new Error(String(error)));
    }
  }

  private accept(frame: TerminalFrame): boolean {
    if (frame.type === "snapshot") {
      this.expected = frame.sequence;
      this.window = frame.inputWindow ?? null;
      this.inFlight = false;
      this.inFlightBytes = 0;
      this.writable = frame.writable;
      this.callbacks.onFrame(frame);
      this.callbacks.onWritable(frame.writable);
      return true;
    }
    if (frame.type === "output") {
      if (this.expected < 0 || frame.sequence !== this.expected + 1) {
        this.fail("protocol");
        return false;
      }
      this.expected = frame.sequence;
      this.callbacks.onFrame(frame);
      return true;
    }
    if (frame.type === "state") {
      this.writable = frame.writable;
      this.callbacks.onFrame(frame);
      this.callbacks.onWritable(frame.writable);
      if (!frame.writable) this.clearQueue();
      return true;
    }
    if (frame.sequence !== this.acknowledged + 1) {
      this.fail("protocol");
      return false;
    }
    this.acknowledged = frame.sequence;
    this.inFlight = false;
    this.inFlightBytes = 0;
    if (frame.refusal !== undefined) {
      this.writable = false;
      this.clearQueue();
      this.callbacks.onWritable(false);
    }
    this.pump();
    return true;
  }

  private fail(issue: "inputFull" | "protocol", error?: unknown): void {
    if (this.closed) return;
    this.callbacks.onIssue?.(issue);
    this.closed = true;
    this.handle.close();
    this.finish(
      "error",
      error instanceof Error
        ? error
        : new Error(issue === "inputFull" ? "terminal input buffer is full" : "terminal input protocol failed"),
    );
  }

  private finish(reason: TerminalEndReason, error?: Error): void {
    if (this.finished) return;
    this.finished = true;
    this.callbacks.onEnd(reason, error);
  }
}
