// `permissionPresets` Remote namespace (packages/interaction/permission-presets).
// The catalog is process-wide: configured preset choices, the subset valid as a
// new-session default, and the effective default preset. Per-session switching
// rides the session projections (`permissions`), not this read.

import { rpc } from "./rpc";

export interface PermissionOption {
  value: string;
  name: string;
  description?: string;
}

export interface PermissionCatalog {
  options: PermissionOption[];
  defaultOptions: PermissionOption[];
  defaultPreset: string;
}

/** Session `permissions` projection value (session-controller `permissions` unit). */
export interface PermissionSelection {
  currentValue: string;
}

/**
 * Read one session's `permissions` projection. `undefined`/malformed values mean
 * the capability is absent on this host (upstream `selectionOf`).
 */
export function permissionSelectionOf(value: unknown): PermissionSelection | null {
  if (typeof value !== "object" || value === null) return null;
  const current = (value as { currentValue?: unknown }).currentValue;
  return typeof current === "string" && current !== "" ? { currentValue: current } : null;
}

export async function loadPermissionCatalog(): Promise<PermissionCatalog> {
  return rpc<PermissionCatalog>("permissionPresets/catalog", {});
}

/** Settings namespace whose `defaultPreset` seeds new sessions. */
export const PERMISSION_SETTINGS_NS = "permission-presets";
