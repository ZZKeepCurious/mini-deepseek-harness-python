// Frame-wide quota notice (src/app/useQuotaNotice.ts): one live notice for the
// whole frame, raised only by a live turn/end quota failure.

import { describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { EventEnvelope } from "../src/wire";
import { useQuotaNotice } from "../src/app/useQuotaNotice";

function failedTurn(seq: number, code: string, message = "provider refused"): EventEnvelope {
  return {
    type: "turn/end",
    seq,
    data: { turn: 1, reason: { kind: "error", error: { code, message } } },
  };
}

describe("useQuotaNotice", () => {
  it("raises one notice for a live QUOTA failure", () => {
    const { result, rerender } = renderHook(
      ({ events }: { events: EventEnvelope[] }) => useQuotaNotice(events),
      { initialProps: { events: [] as EventEnvelope[] } },
    );
    expect(result.current.notice).toBeNull();

    rerender({ events: [failedTurn(1, "QUOTA")] });
    expect(result.current.notice).toMatchObject({ code: "QUOTA" });
  });

  it("ignores other provider failures", () => {
    const { result, rerender } = renderHook(
      ({ events }: { events: EventEnvelope[] }) => useQuotaNotice(events),
      { initialProps: { events: [] as EventEnvelope[] } },
    );
    rerender({ events: [failedTurn(1, "AUTH", "invalid key")] });
    expect(result.current.notice).toBeNull();
  });

  it("ignores failures that were already live before the frame followed them", () => {
    const { result } = renderHook(() => useQuotaNotice([failedTurn(1, "QUOTA")]));
    expect(result.current.notice).toBeNull();
  });

  it("keeps the first notice until dismissed, then reports the next failure", () => {
    const { result, rerender } = renderHook(
      ({ events }: { events: EventEnvelope[] }) => useQuotaNotice(events),
      { initialProps: { events: [] as EventEnvelope[] } },
    );
    rerender({ events: [failedTurn(1, "QUOTA")] });
    const first = result.current.notice;
    expect(first).toMatchObject({ code: "QUOTA" });

    rerender({ events: [failedTurn(1, "QUOTA"), failedTurn(2, "ACCOUNT_QUOTA")] });
    expect(result.current.notice).toBe(first);

    act(() => {
      result.current.dismiss();
    });
    expect(result.current.notice).toBeNull();

    rerender({ events: [failedTurn(1, "QUOTA"), failedTurn(2, "AUTH"), failedTurn(3, "ACCOUNT_QUOTA")] });
    expect(result.current.notice).toMatchObject({ code: "ACCOUNT_QUOTA" });
  });
});
