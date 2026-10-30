// job/follow — read retained output tail for UI preview (§4.7).

import type { JobView } from "./jobs";
import type { StreamHandle } from "./mux";

export type JobFollowFrame =
  | { type: "opened"; job: JobView; from: number }
  | { type: "output"; chunks: Array<{ at: number; text: string }>; next: number }
  | { type: "status"; job: JobView };

function frameOf(value: unknown): JobFollowFrame | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  if (record.type === "opened" || record.type === "status") {
    return record as JobFollowFrame;
  }
  if (record.type === "output" && Array.isArray(record.chunks)) {
    const chunks: Array<{ at: number; text: string }> = [];
    for (const chunk of record.chunks) {
      if (typeof chunk !== "object" || chunk === null) continue;
      const c = chunk as Record<string, unknown>;
      if (typeof c.text === "string") {
        chunks.push({ at: typeof c.at === "number" ? c.at : 0, text: c.text });
      }
    }
    return {
      type: "output",
      chunks,
      next: typeof record.next === "number" ? record.next : 0,
    };
  }
  return null;
}

const PREVIEW_BYTE_LIMIT = 4096;

export async function readJobOutputPreview(
  openStream: (endpoint: string, payload: unknown) => StreamHandle,
  sessionId: string,
  jobId: string,
): Promise<string | null> {
  const stream = openStream("job/follow", { args: { sessionId, jobId } });
  let collected = "";
  try {
    for (;;) {
      const frame = await stream.next();
      if (frame.type === "item") {
        const parsed = frameOf(frame.value);
        if (parsed?.type === "output") {
          for (const chunk of parsed.chunks) {
            collected += chunk.text;
            if (collected.length >= PREVIEW_BYTE_LIMIT) {
              collected = collected.slice(0, PREVIEW_BYTE_LIMIT);
              return collected;
            }
          }
        } else if (parsed?.type === "status") {
          return collected.length > 0 ? collected : null;
        }
      } else if (frame.type === "end" || frame.type === "error") {
        return collected.length > 0 ? collected : null;
      }
    }
  } finally {
    stream.close();
  }
}
