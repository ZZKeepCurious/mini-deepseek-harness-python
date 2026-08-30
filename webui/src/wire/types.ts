// Wire types mirroring the miniharness web transport contract (alpha.1).
// Sources: docs/architecture.md §web + miniharness/web/{stream_protocol,events,streams}.py
// This module is pure data types — no UI, no transport.

// ---------- Event envelope / message model ----------

export interface EventEnvelope {
  type: string;
  seq: number;
  time?: string | number;
  data: Record<string, unknown>;
  surfaceOp?: unknown;
  sourceEventSeqs?: number[];
}

export interface Envelope {
  id: string;
  role: "user" | "assistant" | "system";
  content: ContentBlock[];
  source?: Record<string, unknown>;
}

export type ContentBlock =
  | { type: "text"; text: string }
  | { type: "reasoning"; text: string }
  | { type: "image"; image?: string }
  | { type: "tool-call"; id: string; name: string; arguments: string }
  | {
      type: "tool-result";
      toolCallId: string;
      content: string;
      isError?: boolean;
    };

// ---------- Two-envelope RPC ----------

export interface ClientRequest<T = unknown> {
  type: "client-request";
  rpcId: string;
  method: string;
  payload: T;
}

export interface RpcError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export type RpcResult<T = unknown> =
  | { ok: true; value?: T }
  | { ok: false; error: RpcError };

export interface ServerResponse<T = unknown> {
  type: "server-response";
  rpcId: string;
  result: RpcResult<T>;
}

// ---------- Session list / create ----------

export interface SessionSummary {
  sessionId: string;
  cwd?: string;
  parentSessionId?: string;
  origin?: string;
  running?: boolean;
  blank?: boolean;
  lastPromptAt?: string | number;
  updatedAt?: string | number;
  [key: string]: unknown;
}

export interface SessionListResult {
  items: SessionSummary[];
}

// ---------- follow snapshot ----------

export interface FollowSnapshot {
  type: "snapshot";
  header: Record<string, unknown>;
  cursor: number;
  records: EventEnvelope[];
  hasMore: boolean;
  projections: Record<string, unknown>;
}
