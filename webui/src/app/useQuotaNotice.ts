// One live quota failure publishes one frame-wide notice (upstream
// ui-chat/src/client/apply.ts): only a `turn/end` with an error reason and the
// provider's quota code raises it, and only from a live append — paging or a
// history replacement is not news. A notice already on screen absorbs later
// failures until it is dismissed.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { EventEnvelope } from "../wire";
import { turnEndFailure } from "../trajectory/model";

/** Codes the billing surface claims; other provider failures stay on the turn. */
const QUOTA_CODES: ReadonlySet<string> = new Set(["QUOTA", "ACCOUNT_QUOTA"]);

export interface QuotaNoticeState {
  code: string;
  seq: number;
}

export interface UseQuotaNotice {
  notice: QuotaNoticeState | null;
  dismiss: () => void;
}

/**
 * Track live quota failures over the followed event log.
 * @param events - events appended live after the followed history snapshot.
 * @returns the live notice (if any) and its dismissal.
 */
export function useQuotaNotice(events: readonly EventEnvelope[]): UseQuotaNotice {
  const [notice, setNotice] = useState<QuotaNoticeState | null>(null);
  const noticeRef = useRef<QuotaNoticeState | null>(null);
  /** Live events present before this frame followed the session are already old. */
  const seen = useRef(events.length);
  const sequence = useRef(0);

  useEffect(() => {
    const previous = seen.current;
    seen.current = events.length;
    if (events.length <= previous) return;
    if (noticeRef.current !== null) return;
    for (let index = previous; index < events.length; index++) {
      const failure = turnEndFailure(events[index]);
      if (failure?.code === undefined || !QUOTA_CODES.has(failure.code)) continue;
      const next = { code: failure.code, seq: ++sequence.current };
      noticeRef.current = next;
      setNotice(next);
      return;
    }
  }, [events]);

  const dismiss = useCallback(() => {
    noticeRef.current = null;
    setNotice(null);
  }, []);

  return useMemo(() => ({ notice, dismiss }), [notice, dismiss]);
}
