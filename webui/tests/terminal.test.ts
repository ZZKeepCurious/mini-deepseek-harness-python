import {
  TerminalConnection,
  createTerminalServices,
  type TerminalFrame,
  type TerminalInfo,
} from "../src/wire/terminal";
import type { MuxServerFrame, StreamHandle } from "../src/wire/mux";

const info: TerminalInfo = {
  id: "term-1",
  title: "bash",
  shell: { path: "/bin/bash", name: "bash", args: ["-i"] },
  cwd: "/workspace",
  cols: 80,
  rows: 24,
  state: "running",
  exitCode: null,
};

function fakeHandle() {
  const sent: unknown[] = [];
  const queue: MuxServerFrame[] = [];
  const waiters: Array<(frame: MuxServerFrame) => void> = [];
  let closed = false;
  const handle: StreamHandle & { push: (frame: MuxServerFrame) => void; sent: unknown[] } = {
    streamId: "stream-1",
    sent,
    send: (item) => {
      sent.push(item);
    },
    endUplink: () => undefined,
    close: () => {
      closed = true;
    },
    next: () => {
      if (queue.length > 0) return Promise.resolve(queue.shift()!);
      return new Promise((resolve) => waiters.push(resolve));
    },
    push: (frame) => {
      if (waiters.length > 0) waiters.shift()!(frame);
      else queue.push(frame);
    },
  };
  Object.defineProperty(handle, "closed", { get: () => closed });
  return handle;
}

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe("terminal services", () => {
  it("maps unary calls to the wire agent/session names", async () => {
    const calls: Array<{ method: string; payload: unknown }> = [];
    const opened: Array<{ endpoint: string; payload: unknown }> = [];
    const call = async <T,>(method: string, payload?: unknown): Promise<T> => {
      calls.push({ method, payload });
      return undefined as T;
    };
    const services = createTerminalServices(
      {
        openStream: (endpoint, payload) => {
          opened.push({ endpoint, payload });
          return fakeHandle();
        },
      },
      call,
    );

    await services.environment("s1");
    await services.list("s1");
    await services.create("s1", { id: "t1", cols: 90, rows: 30, shellPath: "/bin/bash" });
    await services.close("s1", "t1");
    services.attach("s1", "t1");
    services.retain("s1", "t1");

    expect(calls).toEqual([
      { method: "terminal/environment", payload: { agentId: "s1" } },
      { method: "terminal/list", payload: { sessionId: "s1" } },
      {
        method: "terminal/create",
        payload: { agentId: "s1", id: "t1", cols: 90, rows: 30, shellPath: "/bin/bash" },
      },
      { method: "terminal/close", payload: { agentId: "s1", id: "t1" } },
    ]);
    expect(opened).toEqual([
      { endpoint: "terminal/attach", payload: { args: { agentId: "s1", id: "t1" } } },
      { endpoint: "terminal/retain", payload: { args: { sessionId: "s1", id: "t1" } } },
    ]);
  });
});

describe("TerminalConnection", () => {
  it("sends input in order and waits for each acknowledgement", async () => {
    const handle = fakeHandle();
    const frames: TerminalFrame[] = [];
    const connection = new TerminalConnection(handle, {
      onFrame: (frame) => frames.push(frame),
      onWritable: () => undefined,
      onEnd: () => undefined,
    });
    connection.setWritable(true);
    connection.write("a");
    connection.write("b");
    expect(handle.sent).toEqual([{ type: "write", sequence: 1, data: "a" }]);

    handle.push({ type: "item", streamId: "stream-1", value: { type: "ack", sequence: 1 } });
    await flush();
    expect(handle.sent).toEqual([
      { type: "write", sequence: 1, data: "a" },
      { type: "write", sequence: 2, data: "b" },
    ]);

    handle.push({ type: "item", streamId: "stream-1", value: { type: "ack", sequence: 2 } });
    await flush();
    connection.close();
    expect(frames).toEqual([]);
  });

  it("renders snapshot and output and rejects an output gap", async () => {
    const handle = fakeHandle();
    const frames: TerminalFrame[] = [];
    const issues: string[] = [];
    let ended: string | null = null;
    const connection = new TerminalConnection(handle, {
      onFrame: (frame) => frames.push(frame),
      onWritable: () => undefined,
      onEnd: (reason) => {
        ended = reason;
      },
      onIssue: (issue) => issues.push(issue),
    });
    handle.push({
      type: "item",
      streamId: "stream-1",
      value: { type: "snapshot", sequence: 4, screen: "ready", info, writable: true },
    });
    handle.push({
      type: "item",
      streamId: "stream-1",
      value: { type: "output", sequence: 5, data: "ok" },
    });
    await flush();
    expect(frames.map((frame) => frame.type)).toEqual(["snapshot", "output"]);

    handle.push({
      type: "item",
      streamId: "stream-1",
      value: { type: "output", sequence: 7, data: "gap" },
    });
    await flush();
    expect(issues).toEqual(["protocol"]);
    expect(ended).toBe("error");
    connection.close();
  });

  it("drops queued input after a refusal and reports read-only", async () => {
    const handle = fakeHandle();
    const writable: boolean[] = [];
    const connection = new TerminalConnection(handle, {
      onFrame: () => undefined,
      onWritable: (next) => writable.push(next),
      onEnd: () => undefined,
    });
    connection.setWritable(true);
    connection.write("a");
    connection.write("b");
    handle.push({
      type: "item",
      streamId: "stream-1",
      value: { type: "ack", sequence: 1, refusal: "read-only" },
    });
    await flush();
    expect(writable).toEqual([false]);
    expect(handle.sent).toHaveLength(1);
    connection.close();
  });
});
