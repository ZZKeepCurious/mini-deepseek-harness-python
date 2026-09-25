// Two-envelope RPC client for the miniharness web transport.
// Contract: POST /api/<endpoint>, body = {type:'client-request', rpcId, method,
// payload} wrapped as {args:{...}} per the gateway strict `{args}` unwrapping.
// Response = server-response {type, rpcId, result:{ok:true,value?}|{ok:false,error}}.
// A result carrying bytes answers with `multipart/form-data` (metadata + one part
// per attachment); it is reassembled into `Uint8Array` before the value is
// returned. Business failures are ALWAYS expressed as result.ok=false (never
// HTTP non-200 besides carrier-level 400/404/415).
// Source: miniharness/web/{envelope,attachments,server}.py.

import type { ClientRequest, RpcError, ServerResponse } from "./types";
import { webToken } from "./auth";

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

  const token = webToken();
  const res = await fetchImpl(`${base}/api/${method}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });

  if (res.status === 200) {
    const mediaType = (res.headers.get("content-type") ?? "")
      .split(";")[0]
      .trim()
      .toLowerCase();
    const message =
      mediaType === "multipart/form-data"
        ? await parseBinaryResponse(res)
        : ((await res.json()) as ServerResponse);
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Reassemble a `multipart/form-data` result: the `metadata` part carries the
 * envelope with `null` at every byte position, and each attachment descriptor
 * names the part to write back along `path`. Every field must be claimed, and
 * every path slot must still hold its placeholder.
 */
async function parseBinaryResponse(res: Response): Promise<ServerResponse> {
  const fields = new Map<string, string | Blob>();
  for (const [name, value] of await res.formData()) {
    if (fields.has(name)) {
      throw new TypeError("connection: invalid binary response fields");
    }
    fields.set(name, value);
  }
  const metadata = fields.get("metadata");
  fields.delete("metadata");
  if (typeof metadata !== "string") {
    throw new TypeError("connection: invalid binary response fields");
  }
  const envelope: unknown = JSON.parse(metadata);
  if (
    !isRecord(envelope) ||
    envelope.type !== "server-response" ||
    typeof envelope.rpcId !== "string"
  ) {
    throw new TypeError("connection: invalid server-response envelope");
  }
  if (!isRecord(envelope.result)) {
    throw new TypeError("connection: invalid server-response result");
  }
  const result = envelope.result;
  if (
    result.ok !== true ||
    !Array.isArray(envelope.attachments) ||
    envelope.attachments.length === 0
  ) {
    throw new TypeError("connection: invalid binary response result");
  }
  const root: { value: unknown } = { value: result.value };
  for (const descriptor of envelope.attachments) {
    if (
      !isRecord(descriptor) ||
      descriptor.codec !== "bytes" ||
      typeof descriptor.part !== "string" ||
      !Array.isArray(descriptor.path)
    ) {
      throw new TypeError("connection: invalid binary response attachment");
    }
    const data = fields.get(descriptor.part);
    fields.delete(descriptor.part);
    if (!(data instanceof Blob)) {
      throw new TypeError("connection: invalid binary response fields");
    }
    let parent: object = root;
    let key: string | number = "value";
    for (const segment of descriptor.path as (string | number)[]) {
      const current: unknown = Reflect.get(parent, key);
      if (typeof current !== "object" || current === null) {
        throw new TypeError("connection: invalid binary response path");
      }
      if (Array.isArray(current)) {
        if (
          typeof segment !== "number" ||
          !Number.isSafeInteger(segment) ||
          segment < 0 ||
          segment >= current.length
        ) {
          throw new TypeError("connection: invalid binary response path");
        }
      } else if (typeof segment !== "string") {
        throw new TypeError("connection: invalid binary response path");
      }
      if (!Object.prototype.hasOwnProperty.call(current, segment)) {
        throw new TypeError("connection: invalid binary response path");
      }
      parent = current;
      key = segment;
    }
    if (Reflect.get(parent, key) !== null) {
      throw new TypeError("connection: invalid binary response placeholder");
    }
    Object.defineProperty(parent, key, {
      value: new Uint8Array(await data.arrayBuffer()),
      enumerable: true,
      writable: true,
      configurable: true,
    });
  }
  if (fields.size !== 0) {
    throw new TypeError("connection: invalid binary response fields");
  }
  return {
    type: "server-response",
    rpcId: envelope.rpcId,
    result: { ok: true, value: root.value },
  };
}
