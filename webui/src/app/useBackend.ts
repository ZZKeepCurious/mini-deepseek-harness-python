// useBackend — React glue between the wire clients and the UI.
// Owns the single remote.mux WebSocket (all streams) and the `$events` client.
// Exposes derived state (sessions, selected trajectory, pending approvals,
// per-session control) plus actions (list/create, follow/select, prompt, ask).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  rpc,
  RemoteMuxConnection,
  RemoteEventClient,
  TrajectoryBuffer,
  type EventEnvelope,
  type SessionSummary,
} from "../wire";

export interface PendingApproval {
  eventId: string;
  agentId: string;
  toolName: string;
}

export interface HostInfo {
  clientId: string;
  home: string;
}

export interface UseBackend {
  ready: boolean;
  host: HostInfo | null;
  connected: boolean;
  sessions: SessionSummary[];
  error: string | null;
  selectedId: string | null;
  trajectory: EventEnvelope[];
  hasMore: boolean;
  running: boolean;
  approvals: PendingApproval[];
  queues: Record<string, unknown[]>;
  jobs: Record<string, unknown[]>;
  refresh: () => Promise<void>;
  createSession: (cwd?: string) => Promise<string | undefined>;
  selectSession: (id: string) => void;
  sendPrompt: (text: string) => Promise<void>;
  askApproval: (approval: PendingApproval, allowed: boolean, sessionId: string) => Promise<void>;
  cancelSession: (id: string) => Promise<void>;
}

export function useBackend(): UseBackend {
  const muxRef = useRef<RemoteMuxConnection | null>(null);
  const eventsRef = useRef<RemoteEventClient | null>(null);
  const followRef = useRef<ReturnType<RemoteMuxConnection["openStream"]> | null>(null);
  const controlRef = useRef<ReturnType<RemoteMuxConnection["openStream"]> | null>(null);
  const bufRef = useRef<TrajectoryBuffer | null>(null);
  if (!bufRef.current) bufRef.current = new TrajectoryBuffer();
  const selectedRef = useRef<string | null>(null);
  const queuesRef = useRef<Record<string, unknown[]>>({});
  const jobsRef = useRef<Record<string, unknown[]>>({});

  const [ready, setReady] = useState(false);
  const [host, setHost] = useState<HostInfo | null>(null);
  const [connected, setConnected] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [, bump] = useState(0);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [running, setRunning] = useState(false);

  const selected = selectedId;

  const attachFollow = useCallback((sessionId: string) => {
    const mux = muxRef.current;
    if (!mux) return;
    followRef.current?.close();
    bufRef.current!.reset();
    const buf = bufRef.current!;
    const stream = mux.openStream(
      "session.follow",
      { args: { address: { kind: "session", sessionId } } },
    );
    followRef.current = stream;
    (async () => {
      for (;;) {
        const frame = await stream.next();
        if (frame.type === "item") {
          const item = (frame as { item: unknown }).item as { type?: string };
          if (item?.type === "snapshot") {
            const snap = item as unknown as {
              records: { type: "event"; event: EventEnvelope }[];
              hasMore: boolean;
              header: Record<string, unknown>;
            };
            // SessionHistoryRecord 包装（上游 entryFor）：{type:'event', event} 解包
            buf.push(snap.records.map((r) => r.event));
            setRunning(Boolean(snap.header?.running));
            bump((n) => n + 1);
          } else if (item?.type === "event") {
            const ev = (item as unknown as { event: EventEnvelope }).event;
            buf.push([ev]);
            bump((n) => n + 1);
          }
        } else if (frame.type === "end" || frame.type === "error") {
          return;
        }
      }
    })();
  }, []);

  const attachControl = useCallback(() => {
    const mux = muxRef.current;
    if (!mux || controlRef.current) return;
    const stream = mux.openStream("session.control", { args: {} });
    controlRef.current = stream;
    (async () => {
      for (;;) {
        const frame = await stream.next();
        if (frame.type === "item") {
          const item = (frame as { item: unknown }).item as {
            type?: string;
            value?: { queues?: Record<string, unknown[]>; jobs?: Record<string, unknown[]> };
            sessionId?: string;
            items?: unknown[];
            jobs?: unknown[];
          };
          if (item?.type === "baseline") {
            queuesRef.current = item.value?.queues ?? {};
            jobsRef.current = item.value?.jobs ?? {};
            bump((n) => n + 1);
          } else if (item?.type === "queue" && item.sessionId) {
            queuesRef.current = { ...queuesRef.current, [item.sessionId]: item.items ?? [] };
            bump((n) => n + 1);
          } else if (item?.type === "jobs" && item.sessionId) {
            jobsRef.current = { ...jobsRef.current, [item.sessionId]: item.jobs ?? [] };
            bump((n) => n + 1);
          }
        } else if (frame.type === "end" || frame.type === "error") {
          return;
        }
      }
    })();
  }, []);

  // Initial connect: mux + $events + list
  useEffect(() => {
    const mux = new RemoteMuxConnection({ url: "/api/remote.mux" });
    mux.connect();
    muxRef.current = mux;

    const events = new RemoteEventClient({
      onReady: (clientId, home) => {
        setHost({ clientId, home });
        setReady(true);
      },
      onWaterfall: (f) => {
        const tool = (f.request?.toolName as string | undefined) ?? (f.request?.name as string | undefined) ?? "";
        setApprovals((a) => [
          ...a,
          { eventId: f.eventId, agentId: f.agentId, toolName: tool },
        ]);
        if (!selectedRef.current) {
          // Auto-follow the approving session the first time.
          if (!selectedRef.current) {
            setSelectedId(f.agentId);
            selectedRef.current = f.agentId;
            attachFollow(f.agentId);
          }
        }
      },
    });

    // wire $events as a stream over the mux: `$events` endpoint
    // (single frame of `ready` then `emit`/`waterfall`/`cancel`)
    const es = mux.openStream("$events", { args: {} });
    (async () => {
      for (;;) {
        const frame = await es.next();
        if (frame.type === "item") {
          events.push((frame as { item: unknown }).item as never);
        } else if (frame.type === "end" || frame.type === "error") {
          return;
        }
      }
    })();
    eventsRef.current = events;
    attachControl();

    (async () => {
      try {
        const list = await rpc<{ items: SessionSummary[] }>("session/list", {});
        setSessions(list.items ?? []);
        setConnected(true);
      } catch (e) {
        setError((e as Error).message);
      }
    })();

    return () => {
      mux.disconnect();
      followRef.current = null;
      controlRef.current = null;
    };
  }, [attachControl, attachFollow]);

  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  const refresh = useCallback(async () => {
    try {
      const list = await rpc<{ items: SessionSummary[] }>("session/list", {});
      setSessions(list.items ?? []);
      setError(null);
      return;
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const createSession = useCallback(async (cwd?: string) => {
    try {
      const created = await rpc<{ sessionId: string }>("session/create", { cwd });
      if (!created?.sessionId) throw new Error("no sessionId");
      await refresh();
      setSelectedId(created.sessionId);
      selectedRef.current = created.sessionId;
      attachFollow(created.sessionId);
      return created.sessionId;
    } catch (e) {
      setError((e as Error).message);
      return undefined;
    }
  }, [attachFollow, refresh]);

  const selectSession = useCallback((id: string) => {
    setSelectedId(id);
    selectedRef.current = id;
    attachFollow(id);
  }, [attachFollow]);

  const sendPrompt = useCallback(async (text: string) => {
    if (!selected) return;
    try {
      await rpc("session/prompt", {
        sessionId: selected,
        requestId: `req-${Date.now()}`,
        mode: "queue",
        content: [{ type: "text", text }],
      });
    } catch (e) {
      setError((e as Error).message);
    }
  }, [selected]);

  const askApproval = useCallback(
    async (approval: PendingApproval, allowed: boolean, sessionId: string) => {
      const events = eventsRef.current;
      if (!events) return;
      await events.resolve(approval.eventId, {
        kind: "result",
        value: allowed
          ? { kind: "allowed-once", sessionId }
          : { kind: "rejected", sessionId, error: { name: "RejectedError", message: "denied by user" } },
      });
      setApprovals((a) => a.filter((x) => x.eventId !== approval.eventId));
    },
    []
  );

  const cancelSession = useCallback(async (id: string) => {
    try {
      await rpc("session/cancel", { sessionId: id });
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const trajectory = bufRef.current ? bufRef.current.events() : [];

  return useMemo(() => {
    return {
      ready,
      host,
      connected,
      sessions,
      error,
      selectedId: selected,
      trajectory,
      hasMore: false,
      running,
      approvals,
      queues: queuesRef.current,
      jobs: jobsRef.current,
      refresh,
      createSession,
      selectSession,
      sendPrompt,
      askApproval,
      cancelSession,
    };
  }, [
    ready, host, connected, sessions, error, selected, trajectory, running,
    approvals, refresh, createSession, selectSession, sendPrompt, askApproval, cancelSession,
  ]);
}
