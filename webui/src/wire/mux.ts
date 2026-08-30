// RemoteStreamMuxConnection client for `/api/remote.mux` (single WebSocket
// carrying all Remote streams). Wire contract (source:
// miniharness/web/mux.py + stream_protocol.py, upstream packages/api/gateway):
//   client → server: {type:'open', streamId, endpoint, payload} | {type:'cancel', ...}
//   server → client: {type:'item', streamId, item} | {type:'error', streamId, error}
//                    | {type:'end', streamId, ok, error?}
// streamId is monotonic (backend allocates; client numbers its own opens).

export type MuxOpen = { type: "open"; streamId: number; endpoint: string; payload: unknown };
export type MuxCancel = { type: "cancel"; streamId: number; endpoint: string; payload: unknown };

export type MuxItem = { type: "item"; streamId: number; item: unknown };
export type MuxError = { type: "error"; streamId: number; error: { code?: string; message?: string } };
export type MuxEnd = { type: "end"; streamId: number; ok: boolean; error?: unknown };

export type MuxServerFrame = MuxItem | MuxError | MuxEnd;

const WS_OPEN = 1;

export interface MuxClientOptions {
  url: string; // e.g. /api/remote.mux
  WebSocketImpl?: typeof WebSocket;
  onFrame?: (frame: MuxServerFrame) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: (ev: Event) => void;
}

export interface StreamHandle {
  readonly streamId: number;
  close: () => void;
}

let lastStreamId = 0;
function nextStreamId(): number {
  return ++lastStreamId;
}

export class RemoteMuxConnection {
  private ws: WebSocket | null = null;
  private pending: Map<number, (frame: MuxServerFrame) => void> = new Map();
  private readonly url: string;
  private readonly WebSocketImpl: typeof WebSocket;
  private onOpen?: () => void;
  private onClose?: () => void;
  private onError?: (ev: Event) => void;
  private disposed = false;

  constructor(opts: MuxClientOptions) {
    this.url = opts.url;
    this.WebSocketImpl = opts.WebSocketImpl ?? WebSocket;
    this.onOpen = opts.onOpen;
    this.onClose = opts.onClose;
    this.onError = opts.onError;
  }

  connect(): void {
    if (this.disposed) return;
    const ws = new this.WebSocketImpl(this.url);
    this.ws = ws;
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      if (this.disposed) return;
      this.onOpen?.();
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (this.disposed) return;
      let frame: MuxServerFrame;
      if (typeof ev.data === "string") {
        frame = JSON.parse(ev.data) as MuxServerFrame;
      } else {
        // Text frame via binary is allowed; try decoding.
        frame = JSON.parse(new TextDecoder().decode(ev.data as ArrayBuffer)) as MuxServerFrame;
      }
      const handler = this.pending.get(frame.streamId);
      if (handler) handler(frame);
    };
    ws.onerror = (ev: Event) => {
      if (this.disposed) return;
      this.onError?.(ev);
    };
    ws.onclose = () => {
      if (this.disposed) return;
      this.disposed = true;
      this.onClose?.();
    };
  }

  /** Open a stream; resolves the first server frame (`item`, `error`, or `end`). */
  openStream(
    endpoint: string,
    payload: unknown,
    onItem?: (item: unknown) => void
  ): StreamHandle & { next: () => Promise<MuxServerFrame> } {
    const streamId = nextStreamId();
    const queue: MuxServerFrame[] = [];
    const waiters: Array<(f: MuxServerFrame) => void> = [];

    const handler = (frame: MuxServerFrame): void => {
      if (frame.type === "item" && onItem) {
        onItem((frame as MuxItem).item);
      }
      if (frame.type !== "item") {
        // error / end resolve any waiter; error/end are terminal-ish.
        const waiter = waiters.shift();
        if (waiter) waiter(frame);
        else queue.push(frame);
        if (frame.type === "end") this.pending.delete(streamId);
      } else {
        const waiter = waiters.shift();
        if (waiter) waiter(frame);
        else queue.push(frame);
      }
    };
    this.pending.set(streamId, handler);

    this.send({ type: "open", streamId, endpoint, payload } satisfies MuxOpen);

    const next = async (): Promise<MuxServerFrame> => {
      if (queue.length) return queue.shift()!;
      return new Promise<MuxServerFrame>((resolve) => waiters.push(resolve));
    };

    return {
      streamId,
      close: () => this.cancel(streamId, endpoint, payload),
      next,
    };
  }

  private send(frame: MuxOpen | MuxCancel): void {
    if (!this.ws || this.ws.readyState !== WS_OPEN) {
      throw new Error("remote.mux: socket not open");
    }
    this.ws.send(JSON.stringify(frame));
  }

  private cancel(streamId: number, endpoint: string, payload: unknown): void {
    try {
      this.send({ type: "cancel", streamId, endpoint, payload } satisfies MuxCancel);
    } catch {
      /* ignore if already closed */
    } finally {
      this.pending.delete(streamId);
    }
  }

  disconnect(): void {
    this.disposed = true;
    this.pending.clear();
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
    this.ws = null;
  }
}
