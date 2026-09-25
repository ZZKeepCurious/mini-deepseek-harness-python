// @vitest-environment node
// Binary result attachments on the unary RPC path: a result carrying bytes
// answers with `multipart/form-data` (metadata part + one part per attachment)
// and the client writes each part back as `Uint8Array` along `path`.
// Node env, not jsdom: `Response.formData()` hands back same-realm File objects
// and rejects a foreign-realm Blob, so multipart bodies only parse here.

import { describe, expect, it, vi } from "vitest";
import { rpc } from "../src/wire";

function multipartBody(
  metadata: string | null,
  parts: Record<string, Uint8Array>
): { body: ArrayBuffer; contentType: string } {
  const boundary = "----miniharnessWireTest";
  const encoder = new TextEncoder();
  const chunks: Uint8Array[] = [];
  const pushText = (text: string) => chunks.push(encoder.encode(text));
  if (metadata !== null) {
    pushText(
      `--${boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n${metadata}\r\n`
    );
  }
  for (const [name, bytes] of Object.entries(parts)) {
    pushText(
      `--${boundary}\r\nContent-Disposition: form-data; name="${name}"; filename="${name}"\r\nContent-Type: application/octet-stream\r\n\r\n`
    );
    chunks.push(bytes);
    pushText("\r\n");
  }
  pushText(`--${boundary}--\r\n`);
  const body = new Uint8Array(chunks.reduce((sum, chunk) => sum + chunk.length, 0));
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.length;
  }
  return { body: body.buffer, contentType: `multipart/form-data; boundary=${boundary}` };
}

function binaryResponse(
  envelope: unknown,
  parts: Record<string, Uint8Array>
): Response {
  const { body, contentType } = multipartBody(JSON.stringify(envelope), parts);
  return new Response(body, { status: 200, headers: { "content-type": contentType } });
}

function okEnvelope(value: unknown, attachments: unknown[]): unknown {
  return {
    type: "server-response",
    rpcId: "x",
    result: { ok: true, value },
    attachments,
  };
}

describe("rpc: binary result attachments", () => {
  it("writes the part back as Uint8Array at its path", async () => {
    const fetchImpl = vi.fn(async () =>
      binaryResponse(
        okEnvelope({ absolutePath: "/a", data: null }, [
          { path: ["data"], codec: "bytes", part: "bytes-0" },
        ]),
        { "bytes-0": new Uint8Array([0, 1, 255]) }
      )
    );
    const value = await rpc<{ data: Uint8Array }>("workspaceFiles/readBytes", {}, {
      fetchImpl,
    });
    expect(value.data).toBeInstanceOf(Uint8Array);
    expect(Array.from(value.data)).toEqual([0, 1, 255]);
  });

  it("reassembles several attachments including array indices", async () => {
    const fetchImpl = vi.fn(async () =>
      binaryResponse(
        okEnvelope({ rows: [null, null], tail: null }, [
          { path: ["rows", 0], codec: "bytes", part: "bytes-0" },
          { path: ["rows", 1], codec: "bytes", part: "bytes-1" },
          { path: ["tail"], codec: "bytes", part: "bytes-2" },
        ]),
        {
          "bytes-0": new Uint8Array([1]),
          "bytes-1": new Uint8Array([2, 2]),
          "bytes-2": new Uint8Array([3]),
        }
      )
    );
    const value = await rpc<{ rows: Uint8Array[]; tail: Uint8Array }>(
      "workspaceFiles/readBytes",
      {},
      { fetchImpl }
    );
    expect(Array.from(value.rows[0])).toEqual([1]);
    expect(Array.from(value.rows[1])).toEqual([2, 2]);
    expect(Array.from(value.tail)).toEqual([3]);
  });

  it("accepts a root-level attachment as the whole value", async () => {
    const fetchImpl = vi.fn(async () =>
      binaryResponse(okEnvelope(null, [{ path: [], codec: "bytes", part: "bytes-0" }]), {
        "bytes-0": new Uint8Array([9]),
      })
    );
    const value = await rpc<Uint8Array>("workspaceFiles/readBytes", {}, { fetchImpl });
    expect(Array.from(value)).toEqual([9]);
  });

  it("rejects a response without a metadata part", async () => {
    const { body, contentType } = multipartBody(null, {});
    const fetchImpl = vi.fn(
      async () =>
        new Response(body, { status: 200, headers: { "content-type": contentType } })
    );
    await expect(rpc("workspaceFiles/readBytes", {}, { fetchImpl })).rejects.toThrow(
      "connection: invalid binary response fields"
    );
  });

  it("rejects a failed result carried as multipart", async () => {
    const fetchImpl = vi.fn(async () =>
      binaryResponse(
        {
          type: "server-response",
          rpcId: "x",
          result: { ok: false, error: { code: "workspace-file/not-found" } },
          attachments: [{ path: ["data"], codec: "bytes", part: "bytes-0" }],
        },
        { "bytes-0": new Uint8Array([1]) }
      )
    );
    await expect(rpc("workspaceFiles/readBytes", {}, { fetchImpl })).rejects.toThrow(
      "connection: invalid binary response result"
    );
  });

  it("rejects an unclaimed part", async () => {
    const fetchImpl = vi.fn(async () =>
      binaryResponse(
        okEnvelope({ data: null }, [{ path: ["data"], codec: "bytes", part: "bytes-0" }]),
        { "bytes-0": new Uint8Array([1]), "bytes-9": new Uint8Array([2]) }
      )
    );
    await expect(rpc("workspaceFiles/readBytes", {}, { fetchImpl })).rejects.toThrow(
      "connection: invalid binary response fields"
    );
  });

  it("rejects a slot that is not the null placeholder", async () => {
    const fetchImpl = vi.fn(async () =>
      binaryResponse(
        okEnvelope({ data: "already" }, [
          { path: ["data"], codec: "bytes", part: "bytes-0" },
        ]),
        { "bytes-0": new Uint8Array([1]) }
      )
    );
    await expect(rpc("workspaceFiles/readBytes", {}, { fetchImpl })).rejects.toThrow(
      "connection: invalid binary response placeholder"
    );
  });

  it("rejects a path index outside the array", async () => {
    const fetchImpl = vi.fn(async () =>
      binaryResponse(
        okEnvelope({ rows: [null] }, [
          { path: ["rows", 3], codec: "bytes", part: "bytes-0" },
        ]),
        { "bytes-0": new Uint8Array([1]) }
      )
    );
    await expect(rpc("workspaceFiles/readBytes", {}, { fetchImpl })).rejects.toThrow(
      "connection: invalid binary response path"
    );
  });
});
