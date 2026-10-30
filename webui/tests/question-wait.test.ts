import { describe, expect, it } from "vitest";
import {
  QuestionCard,
  questionCardKey,
  questionTakeTimeVisible,
  questionWaitOf,
  questionWaitStatus,
} from "../src/wire/question-wait";
import type { QuestionItem } from "../src/wire/questions";

const QUESTIONS: QuestionItem[] = [
  { id: "scope", question: "选择范围", options: [{ label: "仅工具" }, { label: "全部" }] },
];

describe("questionWaitOf", () => {
  it("reads a named timed wait", () => {
    expect(questionWaitOf({ callId: "ask-1", timed: true })).toEqual({
      callId: "ask-1",
      timed: true,
    });
  });

  it("keeps an untimed named wait so the card can hide", () => {
    expect(questionWaitOf({ callId: "ask-1" })).toEqual({ callId: "ask-1", timed: false });
  });

  it.each([
    ["missing callId", { timed: true }],
    ["empty callId", { callId: "", timed: true }],
    ["not an object", "ask-1"],
  ])("declines %s", (_name, value) => {
    expect(questionWaitOf(value)).toBeUndefined();
  });
});

describe("questionCardKey", () => {
  it("keys a named wait by session and call, and falls back to the event id", () => {
    expect(questionCardKey("s1", { callId: "ask-1", timed: true })).toBe("question:s1:ask-1");
    expect(questionCardKey("s1", undefined, "evt-9")).toBe("evt-9");
  });
});

describe("questionWaitStatus", () => {
  it("renders countdown, pause, hold, and continued copy", () => {
    expect(questionWaitStatus("counting", 1500)).toBe("2 秒后继续工作");
    expect(questionWaitStatus("focused", 1500)).toBe("已暂停 · 剩余 2 秒");
    expect(questionWaitStatus("editing", 1500)).toBe("会一直等你回答");
    expect(questionWaitStatus("waiting", 0)).toBe("会一直等你回答");
    expect(questionWaitStatus("continued", 0)).toBe("已继续工作，仍可回答");
  });
});

describe("questionTakeTimeVisible", () => {
  it("offers take-time only while a countdown is still in the foreground", () => {
    expect(questionTakeTimeVisible("counting", true)).toBe(true);
    expect(questionTakeTimeVisible("focused", true)).toBe(true);
    expect(questionTakeTimeVisible("editing", true)).toBe(false);
    expect(questionTakeTimeVisible("waiting", true)).toBe(false);
    expect(questionTakeTimeVisible("continued", true)).toBe(false);
    expect(questionTakeTimeVisible("counting", false)).toBe(false);
  });
});

describe("QuestionCard", () => {
  it("hides a named wait and cancels an unnamed one", () => {
    const named = new QuestionCard({
      eventId: "e1",
      agentId: "s1",
      questions: QUESTIONS,
      wait: { callId: "ask-1", timed: true },
    });
    expect(named.key).toBe("question:s1:ask-1");
    expect(named.dismissal).toBe("hide");
    const unnamed = new QuestionCard({
      eventId: "e2",
      agentId: "s1",
      questions: QUESTIONS,
    });
    expect(unnamed.key).toBe("e2");
    expect(unnamed.dismissal).toBe("cancel");
  });

  it("pauses a pristine countdown on focus and resumes the remainder on blur", () => {
    const card = new QuestionCard({
      eventId: "e1",
      agentId: "s1",
      questions: QUESTIONS,
      wait: { callId: "ask-1", timed: true },
    });
    card.attachDeadline(2000, 10_000);
    expect(card.snapshot(10_000)).toMatchObject({
      waitState: "counting",
      remainingMs: 2000,
      running: true,
      channel: "waterfall",
    });
    card.holdFocus(10_000);
    expect(card.snapshot(12_000)).toMatchObject({
      waitState: "focused",
      remainingMs: 2000,
      running: false,
    });
    expect(card.tick(15_000)).toBeUndefined();
    card.releaseFocus(12_000);
    expect(card.snapshot(12_000)).toMatchObject({ waitState: "counting", running: true });
    expect(card.tick(14_100)).toBe("timeout");
    expect(card.snapshot(14_100)).toMatchObject({
      waitState: "counting",
      channel: "none",
      remainingMs: undefined,
    });
  });

  it("keeps the first edit indefinite", () => {
    const card = new QuestionCard({
      eventId: "e1",
      agentId: "s1",
      questions: QUESTIONS,
      wait: { callId: "ask-1", timed: true },
    });
    card.attachDeadline(1000, 0);
    card.engage(0);
    expect(card.snapshot(0)).toMatchObject({ waitState: "editing", running: false });
    expect(card.tick(5_000)).toBeUndefined();
    expect(card.snapshot(5_000).channel).toBe("waterfall");
  });

  it("lets takeTime drop the deadline and keep the waterfall", () => {
    const card = new QuestionCard({
      eventId: "e1",
      agentId: "s1",
      questions: QUESTIONS,
      wait: { callId: "ask-1", timed: true },
    });
    card.attachDeadline(1000, 0);
    card.takeTime();
    expect(card.snapshot(0)).toMatchObject({ waitState: "waiting", channel: "waterfall" });
    expect(card.tick(5_000)).toBeUndefined();
  });

  it("re-arms through the RPC channel once the projection continues", () => {
    const card = new QuestionCard({
      eventId: "e1",
      agentId: "s1",
      questions: QUESTIONS,
      wait: { callId: "ask-1", timed: true },
    });
    card.attachDeadline(1000, 0);
    expect(card.tick(1_100)).toBe("timeout");
    card.setContinued();
    expect(card.snapshot(1_100)).toMatchObject({
      waitState: "continued",
      channel: "rpc",
    });
  });
});
