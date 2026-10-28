// Bonus notice lifecycle (upstream
// ui-settings-account/src/client/bonus-notices.ts): one foreground card in a
// stack, one read result in force at a time, and one acknowledgement per
// displayed card. Reads happen when the account becomes active and when the
// user asks for a refresh, never on a timer; the server owns which bonus is
// unnotified and what its copy says.
//
// An order is acknowledged once its foreground card reports a presented frame:
// the card has a position, the document is visible, and it painted one frame.
// Closing the card counts as a display too. One lifecycle covers one signed-in
// session: a repeated signed-in frame keeps the card and its acknowledgement
// retries and reads again; signing out, unloading, or a read naming another
// account clears all of it.

import type { AccountBonusBatch, AccountBonusNotification } from "../wire";

/** Notice eligible for display; the message is server-authored for the active locale. */
export interface BonusNotice {
  /** Server-localized plain-text title; absent responses use the localized bonus title. */
  title?: string;
  /** Server order identity. */
  orderId: string;
  /** Server-localized plain text, including any award amount or expiration. */
  message: string;
  /** Server expiration, re-checked at display time; an empty string means no expiration. */
  expiresAt: string;
}

/** Deployment-varying acknowledgement retry timings. */
export interface BonusNoticeTiming {
  /** Delay before the first acknowledgement retry, doubled per further failure. */
  ackRetryDelayMs: number;
  /** Ceiling for the acknowledgement retry backoff. */
  ackRetryMaxDelayMs: number;
}

/** Collaborators and timings of one bonus notice lifecycle. */
export interface BonusNoticeControllerOptions extends BonusNoticeTiming {
  /** Read the signed-in account's unnotified bonus; null when no account is signed in. */
  read: () => Promise<AccountBonusBatch | null>;
  /**
   * Record one order as notified.
   * @returns false when the account changed or signed out; a protocol failure rejects.
   */
  acknowledge: (accountId: string, orderId: string) => Promise<boolean>;
  /** @param notices - notices in front-to-back order; empty when none are shown. */
  publish: (notices: readonly BonusNotice[]) => void;
}

/** Commands the account UI issues to the bonus notice lifecycle. */
export interface BonusNoticeController {
  /** Start the signed-in lifecycle and read. */
  begin: () => void;
  /** Stop the lifecycle, drop the displayed notice, and discard pending acknowledgement retries. */
  end: () => void;
  /** Read the unnotified bonus once, for one Settings entry. */
  refresh: () => Promise<void>;
  /** @param orderId - notice whose card passed a presented frame while visible. */
  shown: (orderId: string) => void;
  /** @param orderId - notice the user closed, which counts as seen. */
  dismiss: (orderId: string) => void;
}

/** Signed-in scope; the account and its pending acknowledgements appear once a read names them. */
interface NoticeScope {
  accountId?: string;
  /** Orders whose card was presented and whose acknowledgement has not settled, oldest first. */
  readonly pending: string[];
}

/**
 * @param expiresAt - server expiration timestamp.
 * @param at - milliseconds since epoch.
 * @returns whether the bonus can no longer be shown.
 */
function expired(expiresAt: string, at: number): boolean {
  if (expiresAt === "") return false;
  const expires = Date.parse(expiresAt);
  // An unparsable timestamp is not evidence of expiry, so the notice still shows.
  return !Number.isNaN(expires) && expires <= at;
}

function noticeOf(notification: AccountBonusNotification): BonusNotice {
  return {
    ...(notification.title === undefined ? {} : { title: notification.title }),
    orderId: notification.orderId,
    message: notification.message,
    expiresAt: notification.expiresAt,
  };
}

/**
 * Own the read, display, and acknowledgement lifecycle of bonus notices.
 * @param options - collaborators and timings.
 * @returns the controller the account hook drives from account state.
 */
export function createBonusNoticeController(
  options: BonusNoticeControllerOptions,
): BonusNoticeController {
  let scope: NoticeScope | undefined;
  /** Notice currently published to the sidebar, including the expiration checked at display time. */
  let current: readonly BonusNotice[] = [];
  /** Newest read; an older read still in flight has its result dropped once a newer one starts. */
  let readGeneration = 0;
  let ackTimer: ReturnType<typeof setTimeout> | undefined;
  let ackInFlight = false;
  let ackDelay = options.ackRetryDelayMs;
  /** Orders the card on screen already reported, so one mount cannot acknowledge twice. */
  const reported = new Set<string>();

  const clearAckTimer = (): void => {
    if (ackTimer !== undefined) {
      clearTimeout(ackTimer);
      ackTimer = undefined;
    }
  };

  const setCurrent = (notices: readonly BonusNotice[]): void => {
    if (
      notices.length === current.length &&
      notices.every((notice, index) => {
        const previous = current[index];
        return (
          notice.orderId === previous?.orderId &&
          notice.message === previous.message &&
          notice.title === previous.title &&
          notice.expiresAt === previous.expiresAt
        );
      })
    ) {
      return;
    }
    const retained = new Set(notices.map((notice) => notice.orderId));
    for (const orderId of reported) if (!retained.has(orderId)) reported.delete(orderId);
    current = notices;
    options.publish(notices);
  };

  const scheduleAck = (delay: number): void => {
    clearAckTimer();
    ackTimer = setTimeout(() => {
      void runAcks();
    }, delay);
  };

  /** Drop acknowledgement work of the previous account or lifecycle. */
  const resetAcks = (): void => {
    clearAckTimer();
    ackInFlight = false;
    ackDelay = options.ackRetryDelayMs;
  };

  const startAcks = (): void => {
    if (scope !== undefined && scope.pending.length > 0) scheduleAck(0);
  };

  /**
   * Apply the server-ordered stack, excluding expired notices. An empty batch
   * retains only the foreground card if the user has already seen it.
   */
  const apply = (scopeAtRead: NoticeScope, batch: AccountBonusBatch): void => {
    if (scopeAtRead.accountId !== batch.accountId) {
      // A different account answered: the previous account's card and pending
      // acknowledgements belong to it and must not outlive its sign-in.
      scopeAtRead.accountId = batch.accountId;
      scopeAtRead.pending.length = 0;
      resetAcks();
      setCurrent([]);
    }
    const candidates = batch.bonuses
      .map(noticeOf)
      .filter((notice) => !expired(notice.expiresAt, Date.now()));
    if (candidates.length === 0) {
      // Keep an already presented foreground card until dismissal; unseen cards are withdrawn.
      const foreground = current[0];
      setCurrent(foreground !== undefined && reported.has(foreground.orderId) ? [foreground] : []);
      return;
    }
    setCurrent(candidates);
  };

  /**
   * Read once for one live lifecycle. A newer read supersedes one in flight: the
   * older result is dropped instead of applied, so only the newest read can report
   * an account change.
   */
  function runRead(scopeAtRead: NoticeScope): Promise<void> {
    const generation = ++readGeneration;
    return (async () => {
      let batch: AccountBonusBatch | null;
      try {
        batch = await options.read();
      } catch {
        // A failed read reports nothing to the user and leaves the lifecycle as
        // it stands, so a pending acknowledgement keeps retrying.
        batch = null;
      }
      if (scope !== scopeAtRead || generation !== readGeneration) return;
      if (batch !== null) apply(scopeAtRead, batch);
    })();
  }

  /**
   * Acknowledge presented orders one at a time. A protocol failure backs off up
   * to the ceiling and retries for the rest of the signed-in lifecycle, which
   * never persists. A false answer settles the order without a retry.
   */
  async function runAcks(): Promise<void> {
    const scopeAtCall = scope;
    if (scopeAtCall === undefined || ackInFlight) return;
    const accountId = scopeAtCall.accountId;
    const orderId = scopeAtCall.pending[0];
    if (accountId === undefined || orderId === undefined) return;
    ackInFlight = true;
    /** @returns whether this call still owns the lifecycle and account it started for. */
    const owns = (): boolean =>
      scope === scopeAtCall && scopeAtCall.accountId === accountId;
    let acknowledged: boolean;
    try {
      acknowledged = await options.acknowledge(accountId, orderId);
    } catch {
      // The lifecycle ended or another account answered while the call was in
      // flight: its pending list, in-flight flag and timer now belong to the new owner.
      if (!owns()) return;
      ackInFlight = false;
      scheduleAck(ackDelay);
      ackDelay = Math.min(ackDelay * 2, options.ackRetryMaxDelayMs);
      return;
    }
    if (!owns()) return;
    ackInFlight = false;
    // Success needs no record and a refusal has no retry, so either way this
    // order is settled for this lifecycle.
    if (scopeAtCall.pending[0] === orderId) scopeAtCall.pending.shift();
    ackDelay = options.ackRetryDelayMs;
    if (acknowledged && scopeAtCall.pending.length > 0) scheduleAck(0);
    else clearAckTimer();
  }

  /** Queue a presented order, so an acknowledgement interrupted by a failure retries within this sign-in. */
  const report = (orderId: string): void => {
    const scopeAtReport = scope;
    if (scopeAtReport === undefined || current[0]?.orderId !== orderId || reported.has(orderId)) {
      return;
    }
    reported.add(orderId);
    if (!scopeAtReport.pending.includes(orderId)) scopeAtReport.pending.push(orderId);
    ackDelay = options.ackRetryDelayMs;
    scheduleAck(0);
  };

  const endLifecycle = (): void => {
    if (scope === undefined) return;
    // The lifecycle a read started for is gone, so its scope check drops the result.
    resetAcks();
    scope = undefined;
    reported.clear();
    setCurrent([]);
  };

  return {
    begin() {
      // Re-entry is the same signed-in session: the card and its acknowledgement
      // retries stay, and this frame's own read supersedes any read still in flight.
      scope ??= { pending: [] };
      void runRead(scope);
    },
    end: endLifecycle,
    refresh(): Promise<void> {
      if (scope === undefined) return Promise.resolve();
      startAcks();
      return runRead(scope);
    },
    shown(orderId) {
      setCurrent(current.filter((notice) => !expired(notice.expiresAt, Date.now())));
      if (current[0]?.orderId !== orderId) return;
      report(orderId);
    },
    dismiss(orderId) {
      setCurrent(current.filter((notice) => !expired(notice.expiresAt, Date.now())));
      if (current[0]?.orderId !== orderId) return;
      // Closing the card is the user seeing it, so queue its acknowledgement before withdrawing.
      report(orderId);
      setCurrent(current.slice(1).filter((notice) => !expired(notice.expiresAt, Date.now())));
    },
  };
}
