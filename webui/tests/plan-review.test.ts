import { describe, expect, it } from "vitest";
import { planReviewOf, planSummaryOf } from "../src/wire/plan-review";
import type { QuestionItem } from "../src/wire/questions";

const PLAN = "# Ship the picker\n\n- read the store\n- render the rows\n";

function questions(): QuestionItem[] {
  return [{
    id: "plan-review",
    header: "Plan review",
    question: "Approve this plan and leave plan mode?",
    detail: PLAN,
    options: [
      { label: "Approve", description: "Leave plan mode; the plan is carried out from the next step." },
      { label: "Keep planning", description: "Stay in plan mode; feedback goes back to the model." },
    ],
    intent: { kind: "plan-review", approve: "Approve" },
  }];
}

describe("planReviewOf", () => {
  it("narrows a plan-review request to its decision, options included", () => {
    expect(planReviewOf(questions())).toEqual({
      id: "plan-review",
      question: "Approve this plan and leave plan mode?",
      plan: PLAN,
      approve: { label: "Approve", description: "Leave plan mode; the plan is carried out from the next step." },
      decline: { label: "Keep planning", description: "Stay in plan mode; feedback goes back to the model." },
    });
  });

  it("retains the logged invocation so the review can reopen its exact plan", () => {
    const [question] = questions();
    const review = planReviewOf([{
      ...question,
      intent: { kind: "plan-review", approve: "Approve", callId: "plan-call" },
    }]);
    expect(review).toEqual({ ...planReviewOf(questions()), callId: "plan-call" });
  });

  it("leaves the decline absent when the asker offered approve alone", () => {
    const [question] = questions();
    const review = planReviewOf([{ ...question, options: [{ label: "Approve" }] }]);
    expect(review?.approve).toEqual({ label: "Approve" });
    expect(review === undefined ? true : "decline" in review).toBe(false);
  });

  it.each([
    ["a batch of more than one question", () => [...questions(), ...questions()]],
    ["no intent at all", () => [{ ...questions()[0], intent: undefined }]],
    ["an intent without the plan as detail", () => [{ ...questions()[0], detail: undefined }]],
    ["an intent whose approve names no option", () => [{
      ...questions()[0],
      intent: { kind: "plan-review", approve: "Ship it" },
    }]],
    ["an intent with no options at all", () => [{ ...questions()[0], options: undefined }]],
    ["a third option the card could not offer", () => [{
      ...questions()[0],
      options: [{ label: "Approve" }, { label: "Keep planning" }, { label: "Start over" }],
    }]],
    ["a multi-select decision", () => [{ ...questions()[0], multiSelect: true }]],
  ])("declines %s, leaving the request to the generic flow", (_name, build) => {
    expect(planReviewOf(build())).toBeUndefined();
  });

  it("declines an empty batch, which the generic flow reports as such", () => {
    expect(planReviewOf([])).toBeUndefined();
  });
});

describe("planSummaryOf", () => {
  it("extracts a plain title and the first paragraph, omitting later steps", () => {
    expect(planSummaryOf(PLAN)).toEqual({
      title: "Ship the picker",
      description: "read the store",
    });
  });

  it("strips emphasis and links from the title and first paragraph", () => {
    const detail = "# **Release** plan\n\nReview the [changes](https://example.com) before **shipping**.\n\n## Steps\n- Build";
    expect(planSummaryOf(detail)).toEqual({
      title: "Release plan",
      description: "Review the changes before shipping.",
    });
  });

  it("keeps a heading-only plan compact", () => {
    expect(planSummaryOf("# Plan without a summary")).toEqual({
      title: "Plan without a summary",
      description: "",
    });
  });

  it("uses a heading-less paragraph as the title once", () => {
    expect(planSummaryOf("A single paragraph plan.")).toEqual({
      title: "A single paragraph plan.",
      description: "",
    });
  });
});
