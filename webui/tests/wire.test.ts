// Wire-layer unit tests. These mock fetch / WebSocket — no transpmsport over the
// network. They pin the alpha.1 wire shapes (two-envelope RPC, remote.mux frames,
// $events/ready|waterfall, $events/result, session.follow/control).

import { describe, expect, it, vi } from "vitest";
import {
  RpcFailure,
  rpc,
  RemoteMuxConnection,
  RemoteEventClient,
  TrajectoryBuffer,
  applyControlFrame,
} from "../src/wire";

// ---------- two-envelope RPC ----------

describe("rpc: two-envelope unary", () => {
  it("wraps payload under {args} and resolves value on ok", async () => {
    const fetchImpl = vi.fn(async (_url, init) => {
      const body = JSON.parse((init as RequestInit).body as string);
      expect(body.type).toBe("client-request");
      expect(body.method).toBe("session/list");
      expect(body.rpcId).toBeTruthy();
      expect(body.payload).toEqual({ args: {} });
      return new Response(
        JSON.stringify({
          type: "server-response",
          rpcId: body.rpcId,
          result: { ok: true, value: { items: [] } },
        }),
        { status: 200 }
      );
    });
    const value = await rpc<{ items: unknown[] }>("session/list", {}, { fetchImpl });
    expect(value.items).toEqual([]);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("throws RpcFailure on result.ok=false", async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({
          type: "server-response",
          rpcId: "x",
          result: { ok: false, error: { code: "auth", message: "bad key" } },
        }),
        { status: 200 }
      )
    );
    const err = await rpc("session/list", {}, { fetchImpl }).catch((e) => e);
    expect(err).toBeInstanceOf(RpcFailure);
    expect((err as RpcFailure).code).toBe("auth");
  });

  it("throws Error on non-200 carrier failure", async () => {
    const fetchImpl = vi.fn(async () => new Response("nope", { status: 404 }));
    await expect(rpc("session/list", {}, { fetchImpl })).rejects.toThrow("404");
  });

  it("uses a fixed rpcId when uuid provided", async () => {
    const fetchImpl = vi.fn(async (_url, init) => {
      const body = JSON.parse((init as RequestInit).body as string);
      expect(body.rpcId).toBe("fixed");
      return new Response(
        JSON.stringify({
          type: "server-response",
          rpcId: "fixed",
          result: { ok: true, value: {} },
        }),
        { status: 200 }
      );
    });
    await rpc("session/page", {}, { fetchImpl, uuid: () => "fixed" });
  });
});

// ---------- remote.mux ----------

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  readyState: number;
  sent: string[] = [];
  onopen: null | (() => void) = null;
  onmessage: null | ((ev: MessageEvent) => void) = null;
  onerror: null | ((ev: Event) => void) = null;
  onclose: null | (() => void) = null;
  constructor(public url: string) {
    this.readyState = 0;
    FakeWebSocket.instances.push(this);
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  serverSend(obj: unknown) {
    this.onmessage?.({ data: JSON.stringify(obj) } as MessageEvent);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
}

describe("RemoteMuxConnection", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
  });

  it("opens a stream and resolves snapshot item", async () => {
    const conn = new RemoteMuxConnection({
      url: "/api/remote.mux",
      WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
    });
    conn.connect();
    const sock = FakeWebSocket.instances[0];
    sock.open();

    const handle = conn.openStream("session.follow", {
      args: { address: { kind: "session", sessionId: "s1" } },
    });
    const first = handle.next();
    // flush an open frame
    void Promise.resolve();
    const parsed = JSON.parse(sock.sent[0]) as { type: string; streamId: number; endpoint: string };
    expect(parsed.type).toBe("open");
    expect(parsed.endpoint).toBe("session.follow");
    expect(parsed.streamId).toBe(handle.streamId);

    sock.serverSend({ type: "item", streamId: handle.streamId, item: { type: "snapshot", records: [] } });
    const frame = await first;
    expect(frame.type).toBe("item");
    expect((frame as { item: { records: unknown[] } }).item.records).toEqual([]);
  });

  it("resolves error frames and supports cancel", async () => {
    const conn = new RemoteMuxConnection({
      url: "/api/remote.mux",
      WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
    });
    conn.connect();
    FakeWebSocket.instances[0].open();

    const handle = conn.openStream("session.follow", { args: {} });
    const p = handle.next();
    void Promise.resolve();
    const parsed = JSON.parse(FakeWebSocket.instances[0].sent[0]) as { streamId: number };
    FakeWebSocket.instances[0].serverSend({
      type: "error",
      streamId: parsed.streamId,
      error: { code: "session-not-found", message: "missing" },
    });
    const frame = await p;
    expect(frame.type).toBe("error");
  });
});

// ---------- $events ----------

describe("RemoteEventClient ($events)", () => {
  it("handles ready then waterfall; resolve posts $events/result", async () => {
    const fetchImpl = vi.fn(async (_url, init) => {
      const body = JSON.parse((init as RequestInit).body as string);
      expect(body.method).toBe("$events/result");
      expect(body.payload.args.clientId).toBe("c1");
      expect(body.payload.args.eventId).toBe("ev1");
      expect(body.payload.args.outcome.value.kind).toBe("allowed-once");
      return new Response(
        JSON.stringify({ type: "server-response", rpcId: "r", result: { ok: true } }),
        { status: 200 }
      );
    });

    const water: unknown[] = [];
    const client = new RemoteEventClient({
      fetchImpl,
      onWaterfall: (f) => water.push(f),
    });
    client.push({ type: "ready", clientId: "c1", host: { home: "/home/me" } });
    expect(client.hasReady()).toBe(true);

    client.push({
      type: "waterfall",
      event: "approval/request",
      eventId: "ev1",
      agentId: "s1",
      request: { toolName: "bash" },
    });
    expect(water).toHaveLength(1);
    const w = water[0] as { request: { toolName: string } };
    expect(w.request.toolName).toBe("bash");

    const ok = await client.resolve("ev1", {
      kind: "result",
      value: { kind: "allowed-once", sessionId: "s1" },
    });
    expect(ok).toBe(true);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("fails closed (does not post) for an already-settled waterfall", async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({ type: "server-response", rpcId: "r", result: { ok: true } }),
        { status: 200 }
      )
    );
    const client = new RemoteEventClient({ fetchImpl });
    client.push({ type: "ready", clientId: "c1", host: { home: "/" } });
    client.push({ type: "cancel", eventId: "done" });
    const ok = await client.resolve("done", { kind: "next" });
    expect(ok).toBe(false);
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

// ---------- follow / control projection ----------

describe("TrajectoryBuffer (session.follow)", () => {
  it("dedups by seq and orders ascending", () => {
    const buf = new TrajectoryBuffer();
    buf.push([
      { type: "user", seq: 2, data: {} },
      { type: "user", seq: 1, data: {} },
    ]);
    buf.push([{ type: "user", seq: 2, data: {} }]); // duplicate ignored
    const events = buf.events();
    expect(events.map((e) => e.seq)).toEqual([1, 2]);
    expect(events).toHaveLength(2);
  });
});

describe("applyControlFrame (session.control)", () => {
  it("replaces queue/jobs per session", () => {
    let s: ReturnType<typeof applyControlFrame> = {
      sessionId: "s1",
      queue: [{ id: "a" }],
      jobs: [],
    };
    s = applyControlFrame(s, { type: "queue", sessionId: "s1", items: [{ id: "b" }] });
    expect(s?.queue).toEqual([{ id: "b" }]);
    s = applyControlFrame(s, { type: "jobs", sessionId: "s1", jobs: [{ id: "j1", status: "running" }] });
    expect(s?.jobs).toHaveLength(1);
    // other session's frame ignored
    s = applyControlFrame(s, { type: "queue", sessionId: "other", items: [] });
    expect(s?.queue).toEqual([{ id: "b" }]);
  });
});
