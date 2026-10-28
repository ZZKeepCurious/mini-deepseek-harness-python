// Bonus notice lifecycle (src/app/bonusNotices.ts): read on activation, one
// foreground card, acknowledgement only after a presented frame, exponential
// backoff on protocol failures, and one lifecycle per signed-in session.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AccountBonusBatch } from "../src/wire";
import type { BonusNotice } from "../src/app/bonusNotices";
import { createBonusNoticeController } from "../src/app/bonusNotices";

function bonus(orderId: string, expiresAt = ""): AccountBonusBatch["bonuses"][number] {
  return { orderId, message: `message ${orderId}`, expiresAt };
}

function batch(accountId: string, ...bonuses: AccountBonusBatch["bonuses"]): AccountBonusBatch {
  return { accountId, bonuses };
}

/** The most recent stack the controller published. */
function latest(published: BonusNotice[][]): BonusNotice[] | undefined {
  return published[published.length - 1];
}

interface Harness {
  controller: ReturnType<typeof createBonusNoticeController>;
  published: BonusNotice[][];
  announcements: { accountId: string; orderId: string }[];
  read: ReturnType<typeof vi.fn>;
  acknowledge: ReturnType<typeof vi.fn>;
}

function harness(options: {
  reads?: Array<AccountBonusBatch | null>;
  acknowledge?: (accountId: string, orderId: string) => Promise<boolean>;
} = {}): Harness {
  const pending = [...(options.reads ?? [])];
  const published: BonusNotice[][] = [];
  const announcements: { accountId: string; orderId: string }[] = [];
  const read = vi.fn(async (): Promise<AccountBonusBatch | null> => {
    if (pending.length === 0) return null;
    return pending.length === 1 ? (pending[0] ?? null) : (pending.shift() ?? null);
  });
  const acknowledge = vi.fn(async (accountId: string, orderId: string) => {
    announcements.push({ accountId, orderId });
    return options.acknowledge === undefined ? true : options.acknowledge(accountId, orderId);
  });
  const controller = createBonusNoticeController({
    ackRetryDelayMs: 1_000,
    ackRetryMaxDelayMs: 30_000,
    read,
    acknowledge,
    publish: (notices) => published.push([...notices]),
  });
  return { controller, published, announcements, read, acknowledge };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-28T00:00:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

describe("bonus notice reads", () => {
  it("publishes the server order without expired awards", async () => {
    const h = harness({
      reads: [
        batch(
          "acct-1",
          bonus("order-live", "2027-01-01T00:00:00Z"),
          bonus("order-expired", "2020-01-01T00:00:00Z"),
          bonus("order-eternal", ""),
        ),
      ],
    });
    h.controller.begin();
    await settle();

    expect(latest(h.published)?.map((notice) => notice.orderId)).toEqual([
      "order-live",
      "order-eternal",
    ]);
    expect(h.read).toHaveBeenCalledTimes(1);
  });

  it("keeps one read result in force, so an identical re-read does not republish", async () => {
    const repeated = batch("acct-1", bonus("order-1"));
    const h = harness({ reads: [repeated, repeated] });
    h.controller.begin();
    await settle();
    h.controller.begin();
    await settle();

    expect(h.read).toHaveBeenCalledTimes(2);
    expect(h.published).toHaveLength(1);
  });

  it("keeps a presented foreground card after an empty batch but withdraws unseen ones", async () => {
    const h = harness({
      reads: [batch("acct-1", bonus("order-1")), batch("acct-1")],
    });
    h.controller.begin();
    await settle();
    h.controller.shown("order-1");
    await settle();
    h.controller.refresh();
    await settle();

    expect(latest(h.published)?.map((notice) => notice.orderId)).toEqual(["order-1"]);

    const unseen = harness({
      reads: [batch("acct-1", bonus("order-9")), batch("acct-1")],
    });
    unseen.controller.begin();
    await settle();
    unseen.controller.refresh();
    await settle();
    expect(unseen.published[unseen.published.length - 1]).toEqual([]);
  });

  it("reads again only while a signed-in lifecycle is active", async () => {
    const h = harness({ reads: [batch("acct-1")] });
    await h.controller.refresh();
    expect(h.read).not.toHaveBeenCalled();

    h.controller.begin();
    await settle();
    h.controller.refresh();
    await settle();
    expect(h.read).toHaveBeenCalledTimes(2);
  });
});

describe("bonus notice acknowledgement", () => {
  it("acknowledges the foreground order only after its card reports a frame", async () => {
    const h = harness({ reads: [batch("acct-1", bonus("order-1"), bonus("order-2"))] });
    h.controller.begin();
    await settle();
    expect(h.acknowledge).not.toHaveBeenCalled();

    // A background card cannot report a display.
    h.controller.shown("order-2");
    await settle();
    expect(h.acknowledge).not.toHaveBeenCalled();

    h.controller.shown("order-1");
    await settle();
    expect(h.announcements).toEqual([{ accountId: "acct-1", orderId: "order-1" }]);

    // One mount cannot acknowledge the same order twice.
    h.controller.shown("order-1");
    await settle();
    expect(h.acknowledge).toHaveBeenCalledTimes(1);
  });

  it("backs off exponentially across protocol failures and keeps retrying in this sign-in", async () => {
    let attempts = 0;
    const h = harness({
      reads: [batch("acct-1", bonus("order-1"))],
      acknowledge: async () => {
        attempts += 1;
        if (attempts < 3) throw new Error("transport closed");
        return true;
      },
    });
    h.controller.begin();
    await settle();
    h.controller.shown("order-1");
    await settle();
    expect(attempts).toBe(1);

    await vi.advanceTimersByTimeAsync(999);
    expect(attempts).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(attempts).toBe(2);

    await vi.advanceTimersByTimeAsync(1_999);
    expect(attempts).toBe(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(attempts).toBe(3);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(attempts).toBe(3);
  });

  it("settles an acknowledged order without a retry when the Host refuses it", async () => {
    const h = harness({
      reads: [batch("acct-1", bonus("order-1"))],
      acknowledge: async () => false,
    });
    h.controller.begin();
    await settle();
    h.controller.shown("order-1");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(h.acknowledge).toHaveBeenCalledTimes(1);
  });

  it("counts closing the card as a display and withdraws it", async () => {
    const h = harness({ reads: [batch("acct-1", bonus("order-1"), bonus("order-2"))] });
    h.controller.begin();
    await settle();
    h.controller.dismiss("order-1");
    expect(latest(h.published)?.map((notice) => notice.orderId)).toEqual(["order-2"]);
    await settle();
    expect(h.announcements).toEqual([{ accountId: "acct-1", orderId: "order-1" }]);
  });

  it("drops the previous account's cards and pending acknowledgements", async () => {
    const h = harness({
      reads: [
        batch("acct-1", bonus("order-1")),
        batch("acct-2", bonus("order-2")),
      ],
      acknowledge: async () => {
        throw new Error("transport closed");
      },
    });
    h.controller.begin();
    await settle();
    h.controller.shown("order-1");
    await settle();
    expect(h.acknowledge).toHaveBeenCalledTimes(1);

    h.controller.refresh();
    await settle();
    expect(latest(h.published)?.map((notice) => notice.orderId)).toEqual(["order-2"]);

    // The previous account's pending order no longer retries.
    await vi.advanceTimersByTimeAsync(60_000);
    expect(h.announcements).toEqual([{ accountId: "acct-1", orderId: "order-1" }]);
  });

  it("clears the card and pending retries when the signed-in lifecycle ends", async () => {
    const h = harness({
      reads: [batch("acct-1", bonus("order-1"))],
      acknowledge: async () => {
        throw new Error("transport closed");
      },
    });
    h.controller.begin();
    await settle();
    h.controller.shown("order-1");
    await settle();
    h.controller.end();
    expect(h.published[h.published.length - 1]).toEqual([]);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(h.acknowledge).toHaveBeenCalledTimes(1);
  });
});
