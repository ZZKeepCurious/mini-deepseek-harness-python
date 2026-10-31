import { useCallback, useEffect, useState } from "react";
import {
  fetchModelCatalog,
  selectSessionModel,
  type ModelCatalog,
  type ModelSelection,
} from "../wire/session";

export interface UseModelCatalog {
  catalog: ModelCatalog | null;
  selection: ModelSelection | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  select: (sessionId: string, provider: string, model: string) => Promise<string | null>;
}

export function useModelCatalog(_sessionId: string | null): UseModelCatalog {
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [selection, setSelection] = useState<ModelSelection | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const value = await fetchModelCatalog();
      setCatalog(value);
      setSelection(value.default);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const select = useCallback(
    async (targetSessionId: string, provider: string, model: string): Promise<string | null> => {
      if (targetSessionId === "") return "缺少会话";
      try {
        const result = await selectSessionModel(targetSessionId, provider, model);
        setSelection(result.selected);
        return null;
      } catch (caught) {
        return caught instanceof Error ? caught.message : String(caught);
      }
    },
    [],
  );

  return { catalog, selection, loading, error, refresh, select };
}
