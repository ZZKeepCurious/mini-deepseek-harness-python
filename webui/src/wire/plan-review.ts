// Plan-review narrowing and markdown summary.
// Source: packages/client/ui-user-questions/src/client/contract/slots.ts planReviewOf
// and packages/client/ui-primitives/src/markdown/plain-text.ts extractMarkdownPlainText.

import type { QuestionItem, QuestionOption } from "./questions";

export interface PlanReview {
  id: string;
  question: string;
  plan: string;
  callId?: string;
  approve: QuestionOption;
  decline?: QuestionOption;
}

export function planReviewOf(questions: readonly QuestionItem[]): PlanReview | undefined {
  if (questions.length !== 1) return undefined;
  const question = questions[0];
  const intent = question.intent;
  if (intent?.kind !== "plan-review" || question.detail === undefined) return undefined;
  if (question.multiSelect === true) return undefined;
  const options = question.options ?? [];
  if (options.length > 2) return undefined;
  const approve = options.find((option) => option.label === intent.approve);
  if (approve === undefined) return undefined;
  const decline = options.find((option) => option.label !== intent.approve);
  return {
    id: question.id,
    question: question.question,
    plan: question.detail,
    ...(intent.callId === undefined ? {} : { callId: intent.callId }),
    approve,
    ...(decline === undefined ? {} : { decline }),
  };
}

function stripInline(text: string): string {
  return text
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .trim();
}

function headingText(line: string): string | null {
  const match = line.match(/^#{1,6}\s+(.+?)\s*$/);
  return match === null ? null : stripInline(match[1]);
}

export function planSummaryOf(plan: string): { title: string; description: string } {
  const lines = plan.replace(/\r\n/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length && lines[index].trim() === "") index += 1;
  if (index >= lines.length) return { title: "", description: "" };
  const heading = headingText(lines[index]);
  if (heading === null) {
    return { title: stripInline(lines[index].replace(/^[-*]\s+/, "")), description: "" };
  }
  index += 1;
  while (index < lines.length && lines[index].trim() === "") index += 1;
  if (index >= lines.length) return { title: heading, description: "" };
  const description = stripInline(lines[index].replace(/^[-*]\s+/, ""));
  return { title: heading, description: description === heading ? "" : description };
}
