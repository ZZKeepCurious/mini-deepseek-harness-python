// Settings dialog state: the redacted describe read, revision-fenced writes,
// the permission catalog, and live refresh on `settings/document-updated`.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  describeSettings,
  loadPermissionCatalog,
  mutateSettings,
  openSettingsDocument,
  settingsConflictOf,
  updateSettings,
  type PermissionCatalog,
  type SettingsConflict,
  type SettingsDescribeView,
  type SettingsNamespaceView,
  type SettingsPathOp,
} from "../wire";
import type { EmitSubscribe } from "./useBackend";

export interface UseSettings {
  snapshot: SettingsDescribeView | null;
  permissionCatalog: PermissionCatalog | null;
  loading: boolean;
  /** Namespace with a write in flight. */
  busyNs: string | null;
  error: string | null;
  conflict: SettingsConflict | null;
  reload: () => Promise<void>;
  update: (
    ns: string,
    patch: Record<string, unknown>,
    expectedRevision?: number,
  ) => Promise<SettingsNamespaceView | null>;
  mutate: (
    ns: string,
    ops: SettingsPathOp[],
    expectedRevision?: number,
  ) => Promise<SettingsNamespaceView | null>;
  openDocument: () => Promise<void>;
  clearConflict: () => void;
  clearError: () => void;
}

function replaceNamespace(
  snapshot: SettingsDescribeView | null,
  next: SettingsNamespaceView,
): SettingsDescribeView | null {
  if (snapshot === null) return snapshot;
  return {
    ...snapshot,
    namespaces: snapshot.namespaces.map((view) => (view.ns === next.ns ? next : view)),
  };
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

export function useSettings(subscribe: EmitSubscribe): UseSettings {
  const [snapshot, setSnapshot] = useState<SettingsDescribeView | null>(null);
  const [permissionCatalog, setPermissionCatalog] = useState<PermissionCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyNs, setBusyNs] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<SettingsConflict | null>(null);
  const generation = useRef(0);

  const reload = useCallback(async () => {
    const token = ++generation.current;
    try {
      const [described, catalog] = await Promise.all([
        describeSettings(),
        loadPermissionCatalog().catch(() => null),
      ]);
      if (token !== generation.current) return;
      setSnapshot(described);
      setPermissionCatalog(catalog);
      setError(null);
    } catch (failure) {
      if (token !== generation.current) return;
      setError(messageOf(failure));
    } finally {
      if (token === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // Another client (or a second tab) edited the document: re-read so every
  // row, revision and secret state reflects the host.
  useEffect(
    () =>
      subscribe("settings/document-updated", () => {
        void reload();
      }),
    [reload, subscribe],
  );

  // The host publishes catalog changes when a permission plugin is added or
  // removed; the settings row must stop offering a preset the process no
  // longer lists. The document itself is unchanged, so only the catalog is
  // re-read.
  useEffect(() => {
    const refreshCatalog = async (): Promise<void> => {
      try {
        setPermissionCatalog(await loadPermissionCatalog());
      } catch {
        setPermissionCatalog(null);
      }
    };
    return subscribe("permission-presets/catalog-changed", () => {
      void refreshCatalog();
    });
  }, [subscribe]);

  const settle = useCallback(
    async (
      ns: string,
      write: () => Promise<SettingsNamespaceView>,
    ): Promise<SettingsNamespaceView | null> => {
      setBusyNs(ns);
      setError(null);
      try {
        const next = await write();
        setSnapshot((current) => replaceNamespace(current, next));
        setConflict(null);
        return next;
      } catch (failure) {
        const stale = settingsConflictOf(failure);
        if (stale !== null) {
          setConflict(stale);
          // The fence is stale: re-read before the user retries.
          await reload();
        } else {
          setError(messageOf(failure));
        }
        return null;
      } finally {
        setBusyNs(null);
      }
    },
    [reload],
  );

  const update = useCallback(
    (ns: string, patch: Record<string, unknown>, expectedRevision?: number) =>
      settle(ns, () => updateSettings(ns, patch, expectedRevision)),
    [settle],
  );

  const mutate = useCallback(
    (ns: string, ops: SettingsPathOp[], expectedRevision?: number) =>
      settle(ns, () => mutateSettings(ns, ops, expectedRevision)),
    [settle],
  );

  const openDocument = useCallback(async () => {
    setError(null);
    try {
      await openSettingsDocument();
    } catch (failure) {
      setError(messageOf(failure));
    }
  }, []);

  return useMemo(
    () => ({
      snapshot,
      permissionCatalog,
      loading,
      busyNs,
      error,
      conflict,
      reload,
      update,
      mutate,
      openDocument,
      clearConflict: () => setConflict(null),
      clearError: () => setError(null),
    }),
    [
      snapshot, permissionCatalog, loading, busyNs, error, conflict,
      reload, update, mutate, openDocument,
    ],
  );
}
