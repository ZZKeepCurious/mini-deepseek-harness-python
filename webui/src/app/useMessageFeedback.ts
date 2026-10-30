import { useCallback, useEffect, useState } from "react";
import {
  deleteMessageFeedback,
  loadMessageFeedback,
  putMessageFeedback,
  type MessageFeedbackState,
  type MessageRating,
} from "../wire/feedback";

export interface UseMessageFeedback {
  state: MessageFeedbackState;
  loading: boolean;
  error: string | null;
  ratingOf: (messageId: string) => MessageRating | undefined;
  setRating: (messageId: string, rating: MessageRating) => Promise<void>;
  clearRating: (messageId: string) => Promise<void>;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function useMessageFeedback(sessionId: string | null): UseMessageFeedback {
  const [state, setState] = useState<MessageFeedbackState>({ items: [] });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (sessionId === null) {
      setState({ items: [] });
      setError(null);
      return;
    }
    setLoading(true);
    try {
      const next = await loadMessageFeedback(sessionId);
      setState(next);
      setError(null);
    } catch (failure) {
      setState({ items: [] });
      setError(messageOf(failure));
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const ratingOf = useCallback(
    (messageId: string) => state.items.find((item) => item.messageId === messageId)?.rating,
    [state.items],
  );

  const setRating = useCallback(
    async (messageId: string, rating: MessageRating) => {
      if (sessionId === null) return;
      const next = await putMessageFeedback(sessionId, messageId, rating, state.version);
      setState(next);
      setError(null);
    },
    [sessionId, state.version],
  );

  const clearRating = useCallback(
    async (messageId: string) => {
      if (sessionId === null) return;
      const next = await deleteMessageFeedback(sessionId, messageId, state.version);
      setState(next);
      setError(null);
    },
    [sessionId, state.version],
  );

  return { state, loading, error, ratingOf, setRating, clearRating };
}
