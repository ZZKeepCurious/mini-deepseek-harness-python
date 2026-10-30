import { afterEach, describe, expect, it } from "vitest";
import {
  clearQuestionDraft,
  encodeQuestionAnswers,
  initialQuestionDrafts,
  parseRecommendedLabel,
  readQuestionDraft,
  writeQuestionDraft,
} from "../src/wire/question-draft";
import type { QuestionItem } from "../src/wire/questions";

afterEach(() => {
  clearQuestionDraft("q-1");
  clearQuestionDraft("q-2");
});

const QUESTIONS: QuestionItem[] = [
  {
    id: "profile",
    header: "偏好",
    question: "选择候选人类型",
    options: [
      { label: "工程落地型 (Recommended)", description: "优先工程交付。" },
      { label: "研究潜力型" },
    ],
  },
  { id: "detail", question: "补充你的要求" },
  {
    id: "signals",
    question: "选择重要信号（可多选）",
    multiSelect: true,
    options: [{ label: "系统设计" }, { label: "代码质量" }],
  },
];

describe("parseRecommendedLabel", () => {
  it("strips a conventional recommendation suffix without changing the answer value", () => {
    expect(parseRecommendedLabel("工程落地型 (Recommended)")).toEqual({
      label: "工程落地型",
      recommended: true,
    });
    expect(parseRecommendedLabel("Fast （推荐）")).toEqual({ label: "Fast", recommended: true });
    expect(parseRecommendedLabel("研究潜力型")).toEqual({
      label: "研究潜力型",
      recommended: false,
    });
  });
});

describe("initialQuestionDrafts", () => {
  it("preselects only a marked first choice", () => {
    expect(initialQuestionDrafts(QUESTIONS)).toEqual([
      { selected: ["工程落地型 (Recommended)"], custom: "", skipped: false },
      { selected: [], custom: "", skipped: false },
      { selected: [], custom: "", skipped: false },
    ]);
  });
});

describe("encodeQuestionAnswers", () => {
  it("encodes skip, custom, single-select, and multi-select into one batch", () => {
    expect(encodeQuestionAnswers(QUESTIONS, [
      { selected: ["研究潜力型"], custom: "", skipped: false },
      { selected: [], custom: "要能独立排查", skipped: false },
      { selected: ["系统设计", "代码质量"], custom: "沟通能力", skipped: false },
    ])).toEqual({
      answers: [
        { id: "profile", selected: ["研究潜力型"] },
        { id: "detail", selected: [], custom: "要能独立排查" },
        { id: "signals", selected: ["系统设计", "代码质量"], custom: "沟通能力" },
      ],
    });
  });

  it("drops a single-select selection when custom text is present", () => {
    expect(encodeQuestionAnswers(QUESTIONS, [
      { selected: ["研究潜力型"], custom: "第一行\n第二行", skipped: false },
      { selected: [], custom: "", skipped: true },
      { selected: [], custom: "", skipped: true },
    ])).toEqual({
      answers: [
        { id: "profile", selected: [], custom: "第一行\n第二行" },
        { id: "detail", selected: [] },
        { id: "signals", selected: [] },
      ],
    });
  });
});

describe("question draft store", () => {
  it("restores page and drafts for the same request key", () => {
    writeQuestionDraft("q-1", {
      index: 1,
      drafts: [
        { selected: ["研究潜力型"], custom: "", skipped: false },
        { selected: [], custom: "保留这段草稿", skipped: false },
        { selected: [], custom: "", skipped: false },
      ],
    });
    expect(readQuestionDraft("q-1", 3)?.index).toBe(1);
    expect(readQuestionDraft("q-1", 3)?.drafts[1]?.custom).toBe("保留这段草稿");
  });

  it("ignores progress that no longer matches the question batch", () => {
    writeQuestionDraft("q-1", { index: 7, drafts: [] });
    expect(readQuestionDraft("q-1", 3)).toBeUndefined();
  });

  it("does not restore one request's draft into another key", () => {
    writeQuestionDraft("q-1", {
      index: 1,
      drafts: [
        { selected: ["stale"], custom: "", skipped: false },
        { selected: [], custom: "", skipped: false },
        { selected: [], custom: "", skipped: false },
      ],
    });
    expect(readQuestionDraft("q-2", 3)).toBeUndefined();
  });
});
