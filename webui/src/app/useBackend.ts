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
  unwrapPageRecords,
  applyControlFrame,
  createTerminalServices,
  createWorkspaceServices,
  type TerminalServices,
  type WorkspaceServices,
  type EventEnvelope,
  type SessionProjections,
  type SessionSummary,
  patchSessionRunning,
  removeSessionSummary,
  sessionSummaryOf,
  upsertSessionSummary,
  type StreamHandle,
  questionItemsOf,
  questionWaitOf,
  activeUserQuestionsOf,
  QuestionCard,
  type QuestionAnswer,
  type QuestionClock,
  type QuestionItem,
  type QuestionWait,
  applyWorkspaceFollowFrame,
  EMPTY_WORKSPACE_ROSTER,
  jobKillArgs,
  readJobOutputPreview,
  workspaceFollowFrameOf,
  type WorkspaceRoster,
} from "../wire";

export interface ApprovalDisplayReason {
  en?: string;
  [locale: string]: string | undefined;
}

export interface PendingApproval {
  eventId: string;
  agentId: string;
  toolName: string;
  /** Tool call correlated with the request, when the asker supplied one. */
  callId?: string;
  /** Audit reason kept verbatim. */
  reason?: string;
  /** Localized presentation copy; the audit reason stays unchanged. */
  displayReason?: ApprovalDisplayReason;
}

/** Decisions this interactive Client presentation can return. */
export type ApprovalOutcome = "allowed-once" | "rejected";

export interface PendingQuestion {
  eventId: string;
  agentId: string;
  key: string;
  questions: QuestionItem[];
  wait?: QuestionWait;
  clock?: QuestionClock;
  hidden?: boolean;
}

function textField(value: unknown): string | undefined {
  return typeof value === "string" && value !== "" ? value : undefined;
}

function displayReasonOf(value: unknown): ApprovalDisplayReason | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const reason: ApprovalDisplayReason = {};
  let present = false;
  for (const [locale, copy] of Object.entries(value)) {
    if (typeof copy !== "string") continue;
    reason[locale] = copy;
    present = true;
  }
  return present ? reason : undefined;
}

export interface HostInfo {
  clientId: string;
  home: string;
}

export type EmitListener = (args: unknown[]) => void;
export type EmitSubscribe = (event: string, listener: EmitListener) => () => void;

export interface UseBackend {
  ready: boolean;
  host: HostInfo | null;
  connected: boolean;
  sessions: SessionSummary[];
  error: string | null;
  selectedId: string | null;
  trajectory: EventEnvelope[];
  /** Events appended live after the followed history snapshot; paging never enters it. */
  liveTrajectory: EventEnvelope[];
  hasMore: boolean;
  running: boolean;
  approvals: PendingApproval[];
  questions: PendingQuestion[];
  projections: SessionProjections;
  jobs: Record<string, unknown[]>;
  workspaces: WorkspaceRoster;
  terminal: TerminalServices;
  workspace: WorkspaceServices;
  /** Open one `remote.mux` logical stream against the live connection. */
  openStream: (endpoint: string, payload: unknown) => StreamHandle;
  refresh: () => Promise<void>;
  createSession: (cwd?: string) => Promise<string | undefined>;
  forkSession: (id: string) => Promise<string | undefined>;
  archiveSession: (id: string) => Promise<void>;
  unarchiveSession: (id: string) => Promise<void>;
  selectSession: (id: string) => void;
  sendPrompt: (text: string) => Promise<void>;
  updateQueue: (itemId: string, action: Record<string, unknown>) => Promise<void>;
  askApproval: (approval: PendingApproval, outcome: ApprovalOutcome) => Promise<void>;
  answerQuestion: (question: PendingQuestion, answer: QuestionAnswer) => Promise<void>;
  dismissQuestion: (question: PendingQuestion) => Promise<void>;
  revealQuestion: (question: PendingQuestion) => void;
  takeTimeQuestion: (question: PendingQuestion) => void;
  holdQuestionFocus: (question: PendingQuestion) => void;
  releaseQuestionFocus: (question: PendingQuestion) => void;
  engageQuestion: (question: PendingQuestion) => void;
  cancelSession: (id: string) => Promise<void>;
  killJob: (jobId: string) => Promise<void>;
  loadEarlierHistory: () => Promise<void>;
  previewJobOutput: (jobId: string) => Promise<string | null>;
  onEmit: EmitSubscribe;
}

export function useBackend(): UseBackend {
  const muxRef = useRef<RemoteMuxConnection | null>(null);
  const eventsRef = useRef<RemoteEventClient | null>(null);
  const followRef = useRef<ReturnType<RemoteMuxConnection["openStream"]> | null>(null);
  const controlRef = useRef<ReturnType<RemoteMuxConnection["openStream"]> | null>(null);
  const jobsRef = useRef<Record<string, unknown[]>>({});
  const bufRef = useRef<TrajectoryBuffer | null>(null);
  if (!bufRef.current) bufRef.current = new TrajectoryBuffer();
  const selectedRef = useRef<string | null>(null);
  const projectionsRef = useRef<SessionProjections>({});
  const questionCardsRef = useRef(new Map<string, QuestionCard>());
  const questionHiddenRef = useRef(new Set<string>());
  const waitStreamsRef = useRef(new Map<string, StreamHandle>());
  const pageThroughRef = useRef(-1);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const terminal = useMemo(
    () =>
      createTerminalServices({
        openStream: (endpoint, payload) => {
          const mux = muxRef.current;
          if (!mux) throw new Error("remote.mux is not connected");
          return mux.openStream(endpoint, payload);
        },
      }),
    [],
  );
  const workspace = useMemo(
    () =>
      createWorkspaceServices({
        openStream: (endpoint, payload) => {
          const mux = muxRef.current;
          if (!mux) throw new Error("remote.mux is not connected");
          return mux.openStream(endpoint, payload);
        },
      }),
    [],
  );
  const openStream = useCallback((endpoint: string, payload: unknown): StreamHandle => {
    const mux = muxRef.current;
    if (!mux) throw new Error("remote.mux is not connected");
    return mux.openStream(endpoint, payload);
  }, []);

  const publishQuestions = useCallback(() => {
    const now = Date.now();
    const next: PendingQuestion[] = [];
    for (const card of questionCardsRef.current.values()) {
      next.push({
        eventId: card.eventId,
        agentId: card.agentId,
        key: card.key,
        questions: card.questions,
        ...(card.wait === undefined ? {} : { wait: card.wait }),
        clock: card.snapshot(now),
        ...(questionHiddenRef.current.has(card.eventId) ? { hidden: true } : {}),
      });
    }
    setQuestions(next);
  }, []);

  const dropQuestion = useCallback((eventId: string) => {
    questionCardsRef.current.delete(eventId);
    questionHiddenRef.current.delete(eventId);
    waitStreamsRef.current.get(eventId)?.close();
    waitStreamsRef.current.delete(eventId);
  }, []);

  const syncContinuedQuestions = useCallback((projections: SessionProjections) => {
    let changed = false;
    for (const card of questionCardsRef.current.values()) {
      if (card.wait === undefined) continue;
      const row = activeUserQuestionsOf(projections[card.agentId]?.userQuestions)
        .find((item) => item.callId === card.wait!.callId);
      if (row?.state === "continued" && card.snapshot(Date.now()).channel !== "rpc") {
        card.setContinued();
        changed = true;
      }
    }
    if (changed) publishQuestions();
  }, [publishQuestions]);

  const [ready, setReady] = useState(false);
  const [host, setHost] = useState<HostInfo | null>(null);
  const [connected, setConnected] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [, bump] = useState(0);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [questions, setQuestions] = useState<PendingQuestion[]>([]);
  const [running, setRunning] = useState(false);
  const [workspaces, setWorkspaces] = useState<WorkspaceRoster>(EMPTY_WORKSPACE_ROSTER);

  const emitListeners = useRef(new Map<string, Set<EmitListener>>());

  const onEmit = useCallback<EmitSubscribe>((event, listener) => {
    const listeners = emitListeners.current;
    let bucket = listeners.get(event);
    if (bucket === undefined) {
      bucket = new Set();
      listeners.set(event, bucket);
    }
    bucket.add(listener);
    return () => {
      bucket.delete(listener);
    };
  }, []);

  const selected = selectedId;

  const attachFollow = useCallback((sessionId: string) => {
    const mux = muxRef.current;
    if (!mux) return;
    followRef.current?.close();
    bufRef.current!.reset();
    const buf = bufRef.current!;
    const stream = mux.openStream(
      "session/follow",
      { args: { address: { kind: "session", sessionId } } },
    );
    followRef.current = stream;
    (async () => {
      for (;;) {
        const frame = await stream.next();
        if (frame.type === "item") {
          const item = (frame as { value: unknown }).value as { type?: string };
          if (item?.type === "snapshot") {
            const snap = item as unknown as {
              records: { type: "event"; event: EventEnvelope }[];
              hasMore: boolean;
              header: Record<string, unknown>;
            };
            // SessionHistoryRecord 包装（上游 entryFor）：{type:'event', event} 解包
            buf.pushHistory(snap.records.map((r) => r.event));
            const cursor = (item as { cursor?: unknown }).cursor;
            if (typeof cursor === "number") pageThroughRef.current = cursor;
            else if (snap.records.length > 0) {
              pageThroughRef.current = snap.records[snap.records.length - 1]!.event.seq;
            }
            setHistoryHasMore(Boolean(snap.hasMore));
            setRunning(Boolean(snap.header?.running));
            bump((n) => n + 1);
          } else if (item?.type === "event") {
            const ev = (item as unknown as { event: EventEnvelope }).event;
            buf.pushLive([ev]);
            bump((n) => n + 1);
          }
        } else if (frame.type === "end" || frame.type === "error") {
          return;
        }
      }
    })();
  }, []);

  const attachJobs = useCallback((sessionId: string) => {
    const mux = muxRef.current;
    if (!mux) return;
    const stream = mux.openStream("job/list", { args: { sessionId } });
    (async () => {
      for (;;) {
        const frame = await stream.next();
        if (frame.type === "item") {
          const value = frame.value as { type?: string; jobs?: unknown[] };
          if (value?.type === "rows") {
            jobsRef.current = { ...jobsRef.current, [sessionId]: value.jobs ?? [] };
            bump((n) => n + 1);
          }
        } else if (frame.type === "end" || frame.type === "error") {
          return;
        }
      }
    })();
  }, []);

  const attachWorkspaces = useCallback(() => {
    const mux = muxRef.current;
    if (!mux) return;
    const stream = mux.openStream("workspace/follow", { args: {} });
    void (async () => {
      for (;;) {
        const frame = await stream.next();
        if (frame.type === "item") {
          const next = workspaceFollowFrameOf(frame.value);
          if (next !== undefined) {
            setWorkspaces((current) => applyWorkspaceFollowFrame(current, next));
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
    const stream = mux.openStream("session/control", { args: {} });
    controlRef.current = stream;
    (async () => {
      for (;;) {
        const frame = await stream.next();
        if (frame.type === "item") {
          projectionsRef.current = applyControlFrame(
            projectionsRef.current,
            frame.value as Parameters<typeof applyControlFrame>[1],
          );
          syncContinuedQuestions(projectionsRef.current);
          bump((n) => n + 1);
        } else if (frame.type === "end" || frame.type === "error") {
          return;
        }
      }
    })();
  }, [syncContinuedQuestions]);

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
      onEmit: (event, args) => {
        if (event === "api-session/added") {
          const row = sessionSummaryOf(args[0]);
          if (row !== null) {
            setSessions((current) => upsertSessionSummary(current, row));
          }
        } else if (event === "api-session/removed") {
          const id = args[0];
          if (typeof id === "string") {
            setSessions((current) => removeSessionSummary(current, id));
          }
        } else if (event === "api-session/status") {
          const id = args[0];
          const running = args[1];
          if (typeof id === "string" && typeof running === "boolean") {
            setSessions((current) => patchSessionRunning(current, id, running));
          }
        }
        const bucket = emitListeners.current.get(event);
        if (bucket === undefined) return;
        for (const listener of [...bucket]) listener(args);
      },
      onWaterfall: (f) => {
        if (f.event === "user-questions/request") {
          const items = questionItemsOf(f.request.questions);
          if (items === undefined || questionCardsRef.current.has(f.eventId)) return;
          const wait = questionWaitOf(f.request.wait);
          const card = new QuestionCard({
            eventId: f.eventId,
            agentId: f.agentId,
            questions: items,
            ...(wait === undefined ? {} : { wait }),
          });
          questionCardsRef.current.set(f.eventId, card);
          publishQuestions();
          if (wait?.timed === true) {
            const stream = mux.openStream("userQuestions/attachWait", {
              args: { agentId: f.agentId, callId: wait.callId },
            });
            waitStreamsRef.current.set(f.eventId, stream);
            void (async () => {
              for (;;) {
                const frame = await stream.next();
                if (frame.type === "item") {
                  const remaining = (frame.value as { remainingMs?: unknown }).remainingMs;
                  if (typeof remaining === "number") {
                    card.attachDeadline(remaining, Date.now());
                    publishQuestions();
                  }
                  continue;
                }
                waitStreamsRef.current.delete(f.eventId);
                return;
              }
            })();
          }
          return;
        }
        const request = f.request;
        const toolName = textField(request.toolName) ?? textField(request.name) ?? "";
        const callId = textField(request.callId);
        const reason = textField(request.reason);
        const displayReason = displayReasonOf(request.displayReason);
        setApprovals((current) => current.some((item) => item.eventId === f.eventId)
          ? current
          : [
            ...current,
            {
              eventId: f.eventId,
              agentId: f.agentId,
              toolName,
              ...(callId === undefined ? {} : { callId }),
              ...(reason === undefined ? {} : { reason }),
              ...(displayReason === undefined ? {} : { displayReason }),
            },
          ]);
      },
      onCancelled: (eventId) => {
        setApprovals((current) => current.filter((item) => item.eventId !== eventId));
        const card = questionCardsRef.current.get(eventId);
        if (card !== undefined && card.snapshot(Date.now()).channel === "none") return;
        dropQuestion(eventId);
        publishQuestions();
      },
    });

    // wire $events as a stream over the mux: `$events` endpoint
    // (single frame of `ready` then `emit`/`waterfall`/`cancel`)
    const es = mux.openStream("$events", { args: {} });
    (async () => {
      for (;;) {
        const frame = await es.next();
        if (frame.type === "item") {
          events.push(frame.value as never);
        } else if (frame.type === "end" || frame.type === "error") {
          return;
        }
      }
    })();
    eventsRef.current = events;
    attachControl();
    attachWorkspaces();

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
      for (const stream of waitStreamsRef.current.values()) stream.close();
      waitStreamsRef.current.clear();
      questionCardsRef.current.clear();
      questionHiddenRef.current.clear();
      mux.disconnect();
      followRef.current = null;
      controlRef.current = null;
    };
  }, [attachControl, attachFollow, attachWorkspaces, dropQuestion, publishQuestions]);

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
      attachJobs(created.sessionId);
      return created.sessionId;
    } catch (e) {
      setError((e as Error).message);
      return undefined;
    }
  }, [attachFollow, refresh]);

  const forkSession = useCallback(async (id: string) => {
    try {
      const created = await rpc<{ sessionId: string }>("session/fork", { sessionId: id });
      if (!created?.sessionId) throw new Error("no sessionId");
      await refresh();
      setSelectedId(created.sessionId);
      selectedRef.current = created.sessionId;
      attachFollow(created.sessionId);
      attachJobs(created.sessionId);
      setError(null);
      return created.sessionId;
    } catch (e) {
      setError((e as Error).message);
      return undefined;
    }
  }, [attachFollow, refresh]);

  const archiveSession = useCallback(async (id: string) => {
    try {
      await rpc("workspace/archiveSession", { sessionId: id });
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const unarchiveSession = useCallback(async (id: string) => {
    try {
      await rpc("workspace/unarchiveSession", { sessionId: id });
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const selectSession = useCallback((id: string) => {
    setSelectedId(id);
    selectedRef.current = id;
    attachFollow(id);
    attachJobs(id);
  }, [attachFollow, attachJobs]);

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

  const updateQueue = useCallback(async (itemId: string, action: Record<string, unknown>) => {
    if (!selected) return;
    try {
      await rpc("session/updateQueue", { sessionId: selected, itemId, action });
    } catch (e) {
      setError((e as Error).message);
    }
  }, [selected]);

  const askApproval = useCallback(
    async (approval: PendingApproval, outcome: ApprovalOutcome) => {
      const events = eventsRef.current;
      if (!events) throw new Error("$events is not connected");
      await events.resolve(approval.eventId, { kind: "result", value: outcome });
      setApprovals((current) => current.filter((item) => item.eventId !== approval.eventId));
    },
    []
  );

  useEffect(() => {
    if (questions.length === 0) return;
    const id = window.setInterval(() => {
      const now = Date.now();
      let changed = false;
      for (const card of questionCardsRef.current.values()) {
        if (card.tick(now) === "timeout") {
          changed = true;
          const events = eventsRef.current;
          if (events) {
            void events.resolve(card.eventId, {
              kind: "rejected",
              error: {
                name: "UserQuestionError",
                message: "ask_user_question timed out before the user answered",
                code: "ASK_TIMED_OUT",
              },
            });
          }
          waitStreamsRef.current.get(card.eventId)?.close();
          waitStreamsRef.current.delete(card.eventId);
        } else if (card.snapshot(now).running === true) {
          changed = true;
        }
      }
      if (changed) publishQuestions();
    }, 250);
    return () => { window.clearInterval(id); };
  }, [questions.length, publishQuestions]);

  const answerQuestion = useCallback(
    async (question: PendingQuestion, answer: QuestionAnswer) => {
      const card = questionCardsRef.current.get(question.eventId);
      const clock = question.clock ?? card?.snapshot(Date.now());
      if (clock?.channel === "rpc") {
        if (question.wait === undefined) throw new Error("continued question is missing callId");
        await rpc("userQuestions/answer", {
          agentId: question.agentId,
          callId: question.wait.callId,
          answer,
        });
      } else {
        const events = eventsRef.current;
        if (!events) throw new Error("$events is not connected");
        await events.resolve(question.eventId, { kind: "result", value: answer });
      }
      dropQuestion(question.eventId);
      publishQuestions();
    },
    [dropQuestion, publishQuestions]
  );

  const dismissQuestion = useCallback(
    async (question: PendingQuestion) => {
      const card = questionCardsRef.current.get(question.eventId);
      if ((question.clock?.dismissal ?? card?.dismissal) === "hide") {
        questionHiddenRef.current.add(question.eventId);
        publishQuestions();
        return;
      }
      const events = eventsRef.current;
      if (!events) throw new Error("$events is not connected");
      await events.resolve(question.eventId, {
        kind: "rejected",
        error: {
          name: "UserQuestionError",
          message: "the user cancelled ask_user_question",
          code: "ASK_CANCELLED",
        },
      });
      dropQuestion(question.eventId);
      publishQuestions();
    },
    [dropQuestion, publishQuestions]
  );

  const revealQuestion = useCallback((question: PendingQuestion) => {
    questionHiddenRef.current.delete(question.eventId);
    publishQuestions();
  }, [publishQuestions]);

  const takeTimeQuestion = useCallback((question: PendingQuestion) => {
    questionCardsRef.current.get(question.eventId)?.takeTime();
    publishQuestions();
  }, [publishQuestions]);

  const holdQuestionFocus = useCallback((question: PendingQuestion) => {
    questionCardsRef.current.get(question.eventId)?.holdFocus(Date.now());
    publishQuestions();
  }, [publishQuestions]);

  const releaseQuestionFocus = useCallback((question: PendingQuestion) => {
    questionCardsRef.current.get(question.eventId)?.releaseFocus(Date.now());
    publishQuestions();
  }, [publishQuestions]);

  const engageQuestion = useCallback((question: PendingQuestion) => {
    questionCardsRef.current.get(question.eventId)?.engage(Date.now());
    publishQuestions();
  }, [publishQuestions]);

  const cancelSession = useCallback(async (id: string) => {
    try {
      await rpc("session/cancel", { sessionId: id });
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const killJob = useCallback(async (jobId: string) => {
    if (!selected) return;
    try {
      await rpc("job/kill", jobKillArgs(selected, jobId));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [selected]);

  const loadEarlierHistory = useCallback(async () => {
    if (!selected || !historyHasMore) return;
    const buf = bufRef.current;
    if (!buf) return;
    const beforeSeq = buf.earliestSeq();
    if (beforeSeq === null) return;
    const throughSeq = pageThroughRef.current;
    if (throughSeq < -1) return;
    try {
      const page = await rpc<{ records?: unknown; hasMore?: boolean }>("session/page", {
        address: { kind: "session", sessionId: selected },
        throughSeq,
        beforeSeq,
        maxMessages: 80,
      });
      const older = unwrapPageRecords(page.records);
      if (older.length > 0) {
        buf.pushHistory(older);
        bump((n) => n + 1);
      }
      setHistoryHasMore(Boolean(page.hasMore));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [selected, historyHasMore]);

  const previewJobOutput = useCallback(async (jobId: string) => {
    if (!selected || !muxRef.current) return null;
    return readJobOutputPreview(
      (endpoint, payload) => muxRef.current!.openStream(endpoint, payload),
      selected,
      jobId,
    );
  }, [selected]);

  const trajectory = bufRef.current ? bufRef.current.events() : [];
  const liveTrajectory = bufRef.current ? bufRef.current.liveEvents() : [];

  return useMemo(() => {
    return {
      ready,
      host,
      connected,
      sessions,
      error,
      selectedId: selected,
      trajectory,
      liveTrajectory,
      hasMore: historyHasMore,
      running,
      approvals,
      questions,
      projections: projectionsRef.current,
      jobs: jobsRef.current,
      workspaces,
      terminal,
      workspace,
      openStream,
      refresh,
      createSession,
      forkSession,
      archiveSession,
      unarchiveSession,
      selectSession,
      sendPrompt,
      updateQueue,
      askApproval,
      answerQuestion,
      dismissQuestion,
      revealQuestion,
      takeTimeQuestion,
      holdQuestionFocus,
      releaseQuestionFocus,
      engageQuestion,
      cancelSession,
      killJob,
      loadEarlierHistory,
      previewJobOutput,
      onEmit,
    };
  }, [
    ready, host, connected, sessions, error, selected, trajectory, liveTrajectory, running,
    historyHasMore, approvals, questions, workspaces, terminal, workspace, openStream, refresh,
    createSession, forkSession, archiveSession, unarchiveSession, selectSession, sendPrompt,
    updateQueue, askApproval, answerQuestion, dismissQuestion, revealQuestion, takeTimeQuestion,
    holdQuestionFocus, releaseQuestionFocus, engageQuestion, cancelSession, killJob,
    loadEarlierHistory, previewJobOutput, onEmit,
  ]);
}
