// Process permission catalog shared by every current-session permission picker.
// Source: upstream packages/client/ui-permission-presets/src/client/catalog.ts —
// one latest-result-wins read, re-read on `permission-presets/catalog-changed`.

import { useCallback, useEffect, useState } from "react";
import { loadPermissionCatalog, type PermissionCatalog } from "../wire";
import type { EmitSubscribe } from "./useBackend";

export interface UsePermissionCatalog {
  catalog: PermissionCatalog | null;
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Subscribe to the host catalog: the first read settles it, and a later
 * `catalog-changed` notification replaces stale options instead of keeping a
 * value the host no longer offers.
 */
export function usePermissionCatalog(subscribe: EmitSubscribe): UsePermissionCatalog {
  const [catalog, setCatalog] = useState<PermissionCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const next = await loadPermissionCatalog();
      setCatalog(next);
      setError(null);
    } catch (failure) {
      setCatalog(null);
      setError(messageOf(failure));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(
    () =>
      subscribe("permission-presets/catalog-changed", () => {
        void reload();
      }),
    [reload, subscribe],
  );

  return { catalog, loading, error, reload };
}
