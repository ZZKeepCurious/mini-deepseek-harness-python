// `user-questions/request` waterfall payload and `$events/result` answers.
// Source: packages/interaction/user-questions/src/types.ts + remotes forward.

export interface QuestionOption {
  label: string;
  description?: string;
}

export interface QuestionIntent {
  kind: string;
  approve: string;
  callId?: string;
}

export interface QuestionItem {
  id: string;
  question: string;
  detail?: string;
  header?: string;
  options?: QuestionOption[];
  multiSelect?: boolean;
  intent?: QuestionIntent;
}

export interface QuestionAnswerItem {
  id: string;
  selected: string[];
  custom?: string;
}

export interface QuestionAnswer {
  answers: QuestionAnswerItem[];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function optionOf(value: unknown): QuestionOption | undefined {
  const record = asRecord(value);
  if (record === null || typeof record.label !== "string") return undefined;
  const option: QuestionOption = { label: record.label };
  if (typeof record.description === "string") option.description = record.description;
  return option;
}

function intentOf(value: unknown): QuestionIntent | undefined {
  const record = asRecord(value);
  if (record === null || typeof record.kind !== "string" || typeof record.approve !== "string") {
    return undefined;
  }
  const intent: QuestionIntent = { kind: record.kind, approve: record.approve };
  if (typeof record.callId === "string") intent.callId = record.callId;
  return intent;
}

export function questionItemsOf(value: unknown): QuestionItem[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const items: QuestionItem[] = [];
  for (const raw of value) {
    const record = asRecord(raw);
    if (record === null || typeof record.id !== "string" || typeof record.question !== "string") {
      return undefined;
    }
    const item: QuestionItem = { id: record.id, question: record.question };
    if (typeof record.detail === "string") item.detail = record.detail;
    if (typeof record.header === "string") item.header = record.header;
    if (record.multiSelect === true) item.multiSelect = true;
    const intent = intentOf(record.intent);
    if (intent !== undefined) item.intent = intent;
    if (Array.isArray(record.options)) {
      const options: QuestionOption[] = [];
      for (const option of record.options) {
        const decoded = optionOf(option);
        if (decoded === undefined) return undefined;
        options.push(decoded);
      }
      item.options = options;
    }
    items.push(item);
  }
  return items;
}
