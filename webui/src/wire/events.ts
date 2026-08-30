// `$events` remote-event client for the miniharness web transport.
// Wire contract (source: miniharness/web/events.py, upstream packages/api/gateway):
//   open $events with payload {args:{}} → first frame {type:'ready', clientId, host:{home}}
//   then: {type:'emit',   event, args[]}          (api-session/* host events)
//         {type:'waterfall', event, eventId, agentId, request}   (host→client async ask)
//         {type:'cancel', eventId}                (settled/withdrawn waterfall)
//   waters are answered via HTTP POST /api/$events/result with
//     body client-request {method:'$events/result', payload:{args:{clientId, eventId,
//           outcome:{kind:'result', value}|{kind:'next'}|{kind:'rejected', error}}}}.

import type { ServerResponse } from "./types";

export type RemoteEventFrame =
  | { type: "ready"; clientId: string; host: { home: string } }
  | { type: "emit"; event: string; args: unknown[] }
  | { type: "waterfall"; event: string; eventId: string; agentId: string; request: unknown }
  | { type: "cancel"; eventId: string };

export interface EventsOptions {
  fetchImpl?: typeof fetch;
  onReady?: (clientId: string, home: string) => void;
  onEmit?: (event: string, args: unknown[]) => void;
  onWaterfall?: (frame: {
    event: string;
    eventId: string;
    agentId: string;
    request: Record<string, unknown>;
  }) => void;
  onCancelled?: (eventId: string) => void;
}

export class RemoteEventClient {
  private frames: RemoteEventFrame[] = [];
  private waiters: Array<(f: RemoteEventFrame) => void> = [];
  private clientId: string | null = null;
  private readonly opts: EventsOptions;
  private readonly fetchImpl: typeof fetch;
  private settled = new Set<string>();

  constructor(opts: EventsOptions = {}) {
    this.opts = opts;
    this.fetchImpl = opts.fetchImpl ?? ((...a) => fetch(...a));
  }

  /** Feed a decoded frame from the mux (in order). */
  push(frame: RemoteEventFrame): void {
    const waiter = this.waiters.shift();
    if (waiter) {
      waiter(frame);
    } else {
      this.frames.push(frame);
    }
    this.dispatch(frame);
  }

  async next(): Promise<RemoteEventFrame> {
    if (this.frames.length) return this.frames.shift()!;
    return new Promise<RemoteEventFrame>((resolve) => this.waiters.push(resolve));
  }

  private dispatch(frame: RemoteEventFrame): void {
    switch (frame.type) {
      case "ready":
        this.clientId = frame.clientId;
        this.opts.onReady?.(frame.clientId, frame.host.home);
        break;
      case "emit":
        this.opts.onEmit?.(frame.event, frame.args);
        break;
      case "waterfall":
        this.opts.onWaterfall?.({
          event: frame.event,
          eventId: frame.eventId,
          agentId: frame.agentId,
          request: (frame.request ?? {}) as Record<string, unknown>,
        });
        break;
      case "cancel":
        this.settled.add(frame.eventId);
        this.opts.onCancelled?.(frame.eventId);
        break;
    }
  }

  hasReady(): boolean {
    return this.clientId !== null;
  }

  getClientId(): string | null {
    return this.clientId;
  }

  /**
   * Answer a pending waterfall via POST /api/$events/result. outcome mirrors the
   * backend's APPROVAL_OUTCOMES handling: allowed-once is the only grant;
   * everything else (rejected/cancelled/unavailable) is fail-closed.
   */
  async resolve(
    eventId: string,
    outcome:
      | { kind: "result"; value: { kind: string; [key: string]: unknown } }
      | { kind: "next" }
      | { kind: "rejected"; error: { name: string; message: string } }
  ): Promise<boolean> {
    if (this.settled.has(eventId)) return false;
    const clientId = this.clientId;
    if (!clientId) throw new Error("$events: not ready");
    const res = await this.fetchImpl("/api/$events/result", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        type: "client-request",
        rpcId: `events-${eventId}-${Date.now()}`,
        method: "$events/result",
        payload: { args: { clientId, eventId, outcome } },
      }),
    });
    if (res.status === 200) {
      const message = (await res.json()) as ServerResponse;
      return Boolean(message?.result?.ok ?? true);
    }
    return false;
  }
}
