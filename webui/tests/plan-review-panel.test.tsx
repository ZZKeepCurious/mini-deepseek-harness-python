import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { PlanReviewPanel } from "../src/ui/PlanReviewPanel";
import { planReviewOf } from "../src/wire/plan-review";
import type { QuestionItem } from "../src/wire/questions";

const PLAN = "# Ship the picker\n\n- read the store\n- render the rows\n";

function items(): QuestionItem[] {
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

function renderPanel(overrides: {
  items?: QuestionItem[];
  onAnswer?: () => Promise<void>;
  onDismiss?: () => Promise<void>;
} = {}) {
  const questions = overrides.items ?? items();
  const review = planReviewOf(questions);
  if (review === undefined) throw new Error("expected a plan review");
  return render(
    <PlanReviewPanel
      pendingKey="q-1"
      review={review}
      onAnswer={overrides.onAnswer ?? (async () => undefined)}
      onDismiss={overrides.onDismiss ?? (async () => undefined)}
    />,
  );
}

describe("PlanReviewPanel", () => {
  it("shows the plan title and summary above two review actions", () => {
    renderPanel();
    expect(document.querySelector("[data-plan-review-key]")?.getAttribute("data-plan-review-key")).toBe("q-1");
    expect(screen.getByText("计划审查")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Ship the picker" })).toBeTruthy();
    expect(screen.getByText("read the store")).toBeTruthy();
    expect(screen.queryByText("render the rows")).toBeNull();
    expect(screen.getByLabelText("Approve this plan and leave plan mode?")).toBeTruthy();
    expect(screen.getAllByRole("button")).toHaveLength(2);
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("answers with the asker's approve label and keeps its description as the tooltip", async () => {
    const onAnswer = vi.fn(async () => undefined);
    renderPanel({ onAnswer });
    const approve = screen.getByRole("button", { name: "批准" });
    expect(approve.getAttribute("title")).toBe("Leave plan mode; the plan is carried out from the next step.");
    fireEvent.click(approve);
    expect(onAnswer).toHaveBeenCalledWith({ answers: [{ id: "plan-review", selected: ["Approve"] }] });
    expect(document.querySelector("[data-plan-review-key] section")?.getAttribute("aria-busy")).toBe("true");
    expect(approve.hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "讨论修改" }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(approve);
    expect(onAnswer).toHaveBeenCalledTimes(1);
  });

  it("dismisses the request so the composer returns for a plain message", () => {
    const onDismiss = vi.fn(async () => undefined);
    const onAnswer = vi.fn(async () => undefined);
    renderPanel({ onDismiss, onAnswer });
    fireEvent.click(screen.getByRole("button", { name: "讨论修改" }));
    expect(onDismiss).toHaveBeenCalledWith();
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("omits the tooltip for an option carrying no description", () => {
    const [question] = items();
    renderPanel({ items: [{ ...question, options: [{ label: "Approve" }, { label: "Keep planning" }] }] });
    expect(screen.getByRole("button", { name: "批准" }).hasAttribute("title")).toBe(false);
  });

  it("re-arms the actions and says why when the decision does not land", async () => {
    const onAnswer = vi.fn(async () => {
      throw new Error("question response rejected: not-pending");
    });
    renderPanel({ onAnswer });
    fireEvent.click(screen.getByRole("button", { name: "批准" }));
    const failure = await screen.findByText("question response rejected: not-pending");
    expect(failure.getAttribute("role")).toBe("status");
    expect(screen.getByRole("button", { name: "批准" }).hasAttribute("disabled")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "批准" }));
    expect(onAnswer).toHaveBeenCalledTimes(2);
  });
});
