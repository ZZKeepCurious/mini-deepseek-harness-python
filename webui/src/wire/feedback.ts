import { rpc } from "./rpc";

export type MessageRating = "up" | "down";

export interface MessageFeedbackItem {
  messageId: string;
  rating: MessageRating;
}

export interface MessageFeedbackState {
  version?: string;
  items: MessageFeedbackItem[];
}

function ratingOf(value: unknown): MessageRating | null {
  if (value === "up" || value === "down") return value;
  if (value === "positive") return "up";
  if (value === "negative") return "down";
  return null;
}

export function parseMessageFeedbackList(value: unknown): MessageFeedbackState {
  if (typeof value !== "object" || value === null) return { items: [] };
  const record = value as Record<string, unknown>;
  const version = typeof record.version === "string" ? record.version : undefined;
  const rawItems = Array.isArray(record.items) ? record.items : [];
  const items: MessageFeedbackItem[] = [];
  for (const raw of rawItems) {
    if (typeof raw !== "object" || raw === null) continue;
    const item = raw as Record<string, unknown>;
    const messageId = typeof item.messageId === "string" ? item.messageId : "";
    const rating = ratingOf(item.rating);
    if (messageId !== "" && rating !== null) items.push({ messageId, rating });
  }
  return { version, items };
}

export async function loadMessageFeedback(sessionId: string): Promise<MessageFeedbackState> {
  const value = await rpc<unknown>("messageFeedback/list", { sessionId });
  return parseMessageFeedbackList(value);
}

export async function putMessageFeedback(
  sessionId: string,
  messageId: string,
  rating: MessageRating,
  ifVersion?: string,
): Promise<MessageFeedbackState> {
  const payload: Record<string, unknown> = {
    sessionId,
    item: { messageId, rating },
  };
  if (ifVersion !== undefined) payload.ifVersion = ifVersion;
  const value = await rpc<unknown>("messageFeedback/put", payload);
  return parseMessageFeedbackList(value);
}

export async function deleteMessageFeedback(
  sessionId: string,
  messageId: string,
  ifVersion?: string,
): Promise<MessageFeedbackState> {
  const payload: Record<string, unknown> = { sessionId, messageId };
  if (ifVersion !== undefined) payload.ifVersion = ifVersion;
  const value = await rpc<unknown>("messageFeedback/delete", payload);
  return parseMessageFeedbackList(value);
}
