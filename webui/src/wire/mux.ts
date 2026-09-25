// RemoteStreamMuxConnection client for `/api/remote.mux` (single WebSocket
// carrying all Remote streams). Wire contract (source:
// miniharness/web/mux.py + stream_protocol.py, upstream packages/api/gateway):
//   client → server: {type:'open', streamId, endpoint, payload}
//                  | {type:'item', streamId, value?}
//                  | {type:'end', streamId}
//                  | {type:'cancel', streamId}
//   server → client: {type:'item', streamId, value} | {type:'error', streamId, error}
//                    | {type:'end', streamId}
// item.value is always present (null is a legal wire value); error is terminal
// (no trailing end frame). streamId is monotonic (backend allocates; client
// numbers its own opens).

import { webToken } from "./auth";
import { isRemoteUplinkItem } from "./json-value";

export type MuxOpen = { type: "open"; streamId: number; endpoint: string; payload: unknown };
export type MuxItemClient = { type: "item"; streamId: number; value?: unknown };
export type MuxEndClient = { type: "end"; streamId: number };
export type MuxCancel = { type: "cancel"; streamId: number };

export type MuxClientFrame = MuxOpen | MuxItemClient | MuxEndClient | MuxCancel;

export type MuxItem = { type: "item"; streamId: number; value: unknown };
export type MuxError = { type: "error"; streamId: number; error: { code?: string; message?: string } };
export type MuxEnd = { type: "end"; streamId: number };

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

/**
 * One open logical stream as this client holds it: the downlink frames plus the
 * uplink of the same stream (upstream `RemoteStreamHandle`, upstream client
 * `ClientStreamHandle`). `openStream` opens the logical stream, so uplink items
 * go out behind the `open` frame.
 */
export interface StreamHandle {
  readonly streamId: number;
  /** Send one uplink item; the Host validates it against the method's uplink codec. */
  send: (item: unknown) => void;
  /** Half-close the uplink, so the Host's uplink iteration ends. Idempotent. */
  endUplink: () => void;
  /** Cancel the logical stream: send `cancel` and stop the downlink iterator. */
  close: () => void;
  /** Next downlink frame (`item`, `error`, or `end`). */
  next: () => Promise<MuxServerFrame>;
}

let lastStreamId = 0;
function nextStreamId(): number {
  return ++lastStreamId;
}

export class RemoteMuxConnection {
  private ws: WebSocket | null = null;
  private pending: Map<number, (frame: MuxServerFrame) => void> = new Map();
  private terminators: Set<(error: Error) => void> = new Set();
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
    const token = webToken();
    const url = token
      ? `${this.url}${this.url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`
      : this.url;
    const ws = new this.WebSocketImpl(url);
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
      this.failAll(new Error("remote.mux: socket closed"));
      this.onClose?.();
    };
  }

  /** Open a stream; `next()` resolves downlink frames (`item`, `error`, or `end`). */
  openStream(
    endpoint: string,
    payload: unknown,
    onItem?: (item: unknown) => void
  ): StreamHandle {
    const streamId = nextStreamId();
    const queue: MuxServerFrame[] = [];
    const waiters: Array<{
      resolve: (f: MuxServerFrame) => void;
      reject: (e: Error) => void;
    }> = [];
    let uplinkEnded = false;
    let terminated = false;
    let failure: Error | null = null;

    const handler = (frame: MuxServerFrame): void => {
      if (frame.type === "item" && onItem) {
        onItem(frame.value);
      }
      // A terminal frame ends the uplink now, not on the next send (upstream
      // stream-client.ts:314-315 stops the pump on any non-item frame).
      if (frame.type !== "item") terminated = true;
      if (frame.type === "end") this.pending.delete(streamId);
      const waiter = waiters.shift();
      if (waiter) waiter.resolve(frame);
      else queue.push(frame);
    };
    this.pending.set(streamId, handler);

    this.send({ type: "open", streamId, endpoint, payload } satisfies MuxOpen);

    const next = (): Promise<MuxServerFrame> => {
      if (queue.length) return Promise.resolve(queue.shift()!);
      if (failure) return Promise.reject(failure);
      return new Promise<MuxServerFrame>((resolve, reject) => waiters.push({ resolve, reject }));
    };

    const terminate = (): void => {
      terminated = true;
      this.pending.delete(streamId);
      this.terminators.delete(fail);
    };

    // Losing the socket terminates every open logical stream: its uplink closes
    // and its downlink reader fails, instead of waiting on a socket that is gone
    // (upstream `failAll` on `lost()`, stream-client.ts:324-333).
    const fail = (error: Error): void => {
      terminated = true;
      failure = error;
      queue.length = 0;
      this.pending.delete(streamId);
      this.terminators.delete(fail);
      while (waiters.length) waiters.shift()!.reject(error);
    };
    this.terminators.add(fail);

    return {
      streamId,
      send: (item: unknown) => {
        if (terminated) {
          throw new Error(`remote.mux: ${endpoint} stream has terminated`);
        }
        if (uplinkEnded) {
          throw new Error(`remote.mux: ${endpoint} uplink was ended`);
        }
        if (!isRemoteUplinkItem(item)) {
          throw new Error(`remote.mux: ${endpoint} uplink item is not a lossless JSON value`);
        }
        // A top-level `undefined` carries no `value` key, which JSON.stringify
        // drops; the Host reads the absent key back as `undefined`.
        this.send({ type: "item", streamId, value: item } satisfies MuxItemClient);
      },
      endUplink: () => {
        if (uplinkEnded || terminated) return;
        uplinkEnded = true;
        this.send({ type: "end", streamId } satisfies MuxEndClient);
      },
      close: () => {
        terminate();
        this.cancel(streamId);
      },
      next,
    };
  }

  private send(frame: MuxClientFrame): void {
    if (!this.ws || this.ws.readyState !== WS_OPEN) {
      throw new Error("remote.mux: socket not open");
    }
    this.ws.send(JSON.stringify(frame));
  }

  private cancel(streamId: number): void {
    try {
      this.send({ type: "cancel", streamId } satisfies MuxCancel);
    } catch {
      /* ignore if already closed */
    } finally {
      this.pending.delete(streamId);
    }
  }

  private failAll(error: Error): void {
    for (const fail of [...this.terminators]) fail(error);
  }

  disconnect(): void {
    this.disposed = true;
    this.failAll(new Error("remote.mux: connection disposed"));
    this.pending.clear();
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
    this.ws = null;
  }
}
