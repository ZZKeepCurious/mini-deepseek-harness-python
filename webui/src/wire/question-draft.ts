// Generic question drafts and answer encoding.
// Source: packages/client/ui-user-questions/src/client/{QuestionComposer.tsx,draft-store.ts}

import type { QuestionAnswer, QuestionItem } from "./questions";

export interface QuestionDraftAnswer {
  selected: string[];
  custom: string;
  skipped: boolean;
}

export interface QuestionDraftProgress {
  index: number;
  drafts: QuestionDraftAnswer[];
}

const draftsByKey = new Map<string, QuestionDraftProgress>();

export function parseRecommendedLabel(label: string): { label: string; recommended: boolean } {
  const suffix = /\s*(?:\((?:recommended|推荐)\)|（(?:recommended|推荐)）)\s*$/i;
  return suffix.test(label)
    ? { label: label.replace(suffix, ""), recommended: true }
    : { label, recommended: false };
}

function recommendedFirstOption(question: QuestionItem): string | undefined {
  const label = question.options?.[0]?.label;
  return label !== undefined && parseRecommendedLabel(label).recommended ? label : undefined;
}

export function initialQuestionDrafts(questions: readonly QuestionItem[]): QuestionDraftAnswer[] {
  return questions.map((item) => {
    const recommended = recommendedFirstOption(item);
    return {
      selected: recommended === undefined ? [] : [recommended],
      custom: "",
      skipped: false,
    };
  });
}

function isQuestionDraftProgress(value: unknown, questionCount: number): value is QuestionDraftProgress {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const progress = value as Record<string, unknown>;
  if (
    typeof progress.index !== "number"
    || !Number.isInteger(progress.index)
    || progress.index < 0
    || progress.index >= questionCount
    || !Array.isArray(progress.drafts)
    || progress.drafts.length !== questionCount
  ) {
    return false;
  }
  return progress.drafts.every((item: unknown) => {
    if (typeof item !== "object" || item === null || Array.isArray(item)) return false;
    const draft = item as Record<string, unknown>;
    return Array.isArray(draft.selected)
      && draft.selected.every((label: unknown) => typeof label === "string")
      && typeof draft.custom === "string"
      && typeof draft.skipped === "boolean";
  });
}

export function readQuestionDraft(key: string, questionCount: number): QuestionDraftProgress | undefined {
  const stored = draftsByKey.get(key);
  return isQuestionDraftProgress(stored, questionCount) ? stored : undefined;
}

export function writeQuestionDraft(key: string, progress: QuestionDraftProgress): void {
  draftsByKey.set(key, progress);
}

export function clearQuestionDraft(key: string): void {
  draftsByKey.delete(key);
}

export function answeredDraft(item: QuestionDraftAnswer): boolean {
  return item.selected.length > 0 || item.custom.trim() !== "";
}

export function completedDraft(item: QuestionDraftAnswer): boolean {
  return answeredDraft(item) || item.skipped;
}

export function encodeQuestionAnswers(
  questions: readonly QuestionItem[],
  drafts: readonly QuestionDraftAnswer[],
): QuestionAnswer {
  return {
    answers: questions.map((item, itemIndex) => {
      const value = drafts[itemIndex] as QuestionDraftAnswer;
      if (value.skipped) return { id: item.id, selected: [] };
      const custom = value.custom.trim();
      return {
        id: item.id,
        selected: custom === "" || item.multiSelect === true ? value.selected : [],
        ...(custom === "" ? {} : { custom }),
      };
    }),
  };
}
