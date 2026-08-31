// Two-envelope RPC client for the miniharness web transport.
// Contract: POST /api/<endpoint>, body = {type:'client-request', rpcId, method,
// payload} wrapped as {args:{...}} per the gateway strict `{args}` unwrapping.
// Response = server-response {type, rpcId, result:{ok:true,value?}|{ok:false,error}}.
// Business failures are ALWAYS expressed as result.ok=false (never HTTP non-200
// besides carrier-level 400/404/415). Source: miniharness/web/{envelope,server}.py.

import type { ClientRequest, RpcError, ServerResponse } from "./types";

export class RpcFailure extends Error {
  readonly code: string;
  readonly details?: Record<string, unknown>;
  constructor(error: RpcError) {
    super(`${error.code}: ${error.message}`);
    this.name = "RpcFailure";
    this.code = error.code;
    this.details = error.details;
  }
}

export interface RpcOptions {
  base?: string; // default '' → same origin; "/api/..." appended
  fetchImpl?: typeof fetch;
  uuid?: () => string;
}

let requestCounter = 0;

// The gateway requires the unary payload to be exactly `{args:{...}}`.
function wrapArgs(payload: unknown): unknown {
  return { args: payload === undefined ? {} : payload };
}

/**
 * Perform a unary RPC. Returns the resolved value (result.ok → value) or throws
 * RpcFailure for result.ok=false. Throws Error for carrier-level failures.
 */
export async function rpc<T = unknown>(
  method: string,
  payload: unknown = {},
  opts: RpcOptions = {}
): Promise<T> {
  const base = opts.base ?? "";
  const uuid = opts.uuid ?? (() =>
    (typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `rpc-${++requestCounter}-${Date.now()}`));
  const fetchImpl = opts.fetchImpl ?? ((...a: Parameters<typeof fetch>) => fetch(...a));

  const body: ClientRequest = {
    type: "client-request",
    rpcId: uuid(),
    method,
    payload: wrapArgs(payload),
  };

  const res = await fetchImpl(`${base}/api/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

  if (res.status === 200) {
    const message = (await res.json()) as ServerResponse;
    const result = message?.result;
    if (!result?.ok) {
      throw new RpcFailure(
        (result as { error?: RpcError } | undefined)?.error ?? {
          code: "gateway/internal",
          message: "no error payload",
        }
      );
    }
    return (result as { value?: T }).value as T;
  }

  let detail = `HTTP ${res.status}`;
  try {
    const text = await res.text();
    if (text.trim()) detail = `HTTP ${res.status}: ${text.trim()}`;
  } catch {
    /* ignore */
  }
  throw new Error(`${method} → ${detail}`);
}
