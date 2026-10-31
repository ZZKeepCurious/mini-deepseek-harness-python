import { useCallback, useEffect, useState } from "react";
import {
  deleteSessionSchedule,
  loadScheduleCatalog,
  loadSessionSchedules,
  updateSessionSchedule,
  type ScheduleCatalogEntry,
  type ScheduleRecord,
} from "../wire/schedule";
import type { EmitSubscribe } from "./useBackend";

export interface UseSchedules {
  schedules: ScheduleRecord[];
  catalog: ScheduleCatalogEntry[];
  loading: boolean;
  catalogLoading: boolean;
  error: string | null;
  catalogError: string | null;
  reload: () => Promise<void>;
  reloadCatalog: () => Promise<void>;
  remove: (id: string, sessionIdOverride?: string) => Promise<void>;
  update: (
    id: string,
    expected: ScheduleRecord,
    patch: { title?: string; prompt?: string },
    sessionIdOverride?: string,
  ) => Promise<boolean>;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function useSchedules(sessionId: string | null, subscribe: EmitSubscribe): UseSchedules {
  const [schedules, setSchedules] = useState<ScheduleRecord[]>([]);
  const [catalog, setCatalog] = useState<ScheduleCatalogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);

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

  const reloadCatalog = useCallback(async () => {
    setCatalogLoading(true);
    try {
      const next = await loadScheduleCatalog();
      setCatalog(next);
      setCatalogError(null);
    } catch (failure) {
      setCatalog([]);
      setCatalogError(messageOf(failure));
    } finally {
      setCatalogLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    void reloadCatalog();
  }, [reloadCatalog]);

  useEffect(
    () =>
      subscribe("schedule/changed", () => {
        void reload();
        void reloadCatalog();
      }),
    [subscribe, reload, reloadCatalog],
  );

  const remove = useCallback(
    async (id: string, sessionIdOverride?: string) => {
      const target = sessionIdOverride ?? sessionId;
      if (target === null) return;
      try {
        const ok = await deleteSessionSchedule(target, id);
        setError(ok ? null : "日程不存在或已删除");
      } catch (failure) {
        setError(messageOf(failure));
      }
      await reload();
      await reloadCatalog();
    },
    [sessionId, reload, reloadCatalog],
  );

  const update = useCallback(
    async (
      id: string,
      expected: ScheduleRecord,
      patch: { title?: string; prompt?: string },
      sessionIdOverride?: string,
    ) => {
      const target = sessionIdOverride ?? sessionId;
      if (target === null) return false;
      try {
        const ok = await updateSessionSchedule(target, id, expected, patch);
        if (!ok) setError("日程更新冲突或已结束");
        else setError(null);
        await reload();
        await reloadCatalog();
        return ok;
      } catch (failure) {
        setError(messageOf(failure));
        return false;
      }
    },
    [sessionId, reload, reloadCatalog],
  );

  return {
    schedules,
    catalog,
    loading,
    catalogLoading,
    error,
    catalogError,
    reload,
    reloadCatalog,
    remove,
    update,
  };
}
