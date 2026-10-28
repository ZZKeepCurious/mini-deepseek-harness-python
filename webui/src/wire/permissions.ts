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

export async function loadPermissionCatalog(): Promise<PermissionCatalog> {
  return rpc<PermissionCatalog>("permissionPresets/catalog", {});
}

/** Settings namespace whose `defaultPreset` seeds new sessions. */
export const PERMISSION_SETTINGS_NS = "permission-presets";
