import { useCallback, useEffect, useState } from "react";
import {
  deleteSessionSchedule,
  loadSessionSchedules,
  type ScheduleRecord,
} from "../wire/schedule";
import type { EmitSubscribe } from "./useBackend";

export interface UseSchedules {
  schedules: ScheduleRecord[];
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
  remove: (id: string) => Promise<void>;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function useSchedules(sessionId: string | null, subscribe: EmitSubscribe): UseSchedules {
  const [schedules, setSchedules] = useState<ScheduleRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (sessionId === null) {
      setSchedules([]);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const next = await loadSessionSchedules(sessionId);
      setSchedules(next);
      setError(null);
    } catch (failure) {
      setSchedules([]);
      setError(messageOf(failure));
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(
    () =>
      subscribe("schedule/changed", () => {
        void reload();
      }),
    [subscribe, reload],
  );

  const remove = useCallback(
    async (id: string) => {
      if (sessionId === null) return;
      try {
        const ok = await deleteSessionSchedule(sessionId, id);
        setError(ok ? null : "日程不存在或已删除");
      } catch (failure) {
        setError(messageOf(failure));
      }
      await reload();
    },
    [sessionId, reload],
  );

  return { schedules, loading, error, reload, remove };
}
