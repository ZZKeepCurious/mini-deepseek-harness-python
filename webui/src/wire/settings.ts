// `settings` Remote namespace (packages/api/settings-controller).
// Contract: settings/describe returns every live plugin namespace under a
// redacted read ({writable, hasDocument, namespaces}); each namespace carries
// its schema, resolved value, three layers (base/user), revision and the
// secret paths the host stripped. Writes are path-addressed mutations fenced by
// the revision the caller read; a stale fence answers `settings/conflict` with
// {ns, expected, actual}, and any other refusal is `settings/rejected`.
// `settings/document-updated` rides `$events` as `[ns, revision]`.

import { RpcFailure, rpc } from "./rpc";

export type SettingsPath = (string | number)[];

/** One secret field: its path inside the section plus whether it is stored. */
export interface SettingsSecretView {
  path: SettingsPath;
  set: boolean;
}

/** One plugin namespace as the redacted describe read projects it. */
export interface SettingsNamespaceView {
  ns: string;
  autoGenerate: boolean;
  /** JSON Schema of the plugin's config; `{}` when the plugin declares none. */
  schema: Record<string, unknown>;
  value: unknown;
  applies: string;
  secrets: SettingsSecretView[];
  revision: number;
  /** Inherited (non-user) layer; omitted when empty. */
  base?: unknown;
  /** User patch layer; omitted when empty. */
  user?: unknown;
}

export interface SettingsDescribeView {
  writable: boolean;
  hasDocument: boolean;
  namespaces: SettingsNamespaceView[];
}

/** One path-addressed write accepted by settings/mutate. */
export type SettingsPathOp =
  | { op: "set"; path: SettingsPath; value: unknown }
  | { op: "unset"; path: SettingsPath };

export interface SettingsConflict {
  ns: string;
  expected?: number;
  actual?: number;
  message: string;
}

export async function describeSettings(): Promise<SettingsDescribeView> {
  return rpc<SettingsDescribeView>("settings/describe", {});
}

export async function updateSettings(
  ns: string,
  patch: Record<string, unknown>,
  expectedRevision?: number,
): Promise<SettingsNamespaceView> {
  return rpc<SettingsNamespaceView>("settings/update", {
    ns,
    patch,
    ...(expectedRevision === undefined ? {} : { expectedRevision }),
  });
}

export async function replaceSettings(
  ns: string,
  section: Record<string, unknown>,
  expectedRevision?: number,
): Promise<SettingsNamespaceView> {
  return rpc<SettingsNamespaceView>("settings/replace", {
    ns,
    section,
    ...(expectedRevision === undefined ? {} : { expectedRevision }),
  });
}

export async function mutateSettings(
  ns: string,
  ops: SettingsPathOp[],
  expectedRevision?: number,
): Promise<SettingsNamespaceView> {
  return rpc<SettingsNamespaceView>("settings/mutate", {
    ns,
    ops,
    ...(expectedRevision === undefined ? {} : { expectedRevision }),
  });
}

/** Open the host settings document in the deployment's text editor. */
export async function openSettingsDocument(): Promise<{ opened: boolean }> {
  return rpc<{ opened: boolean }>("settings/openSettingsDocument", {});
}

/** Project an RPC failure onto the conflict details when the code matches. */
export function settingsConflictOf(error: unknown): SettingsConflict | null {
  if (!(error instanceof RpcFailure) || error.code !== "settings/conflict") return null;
  const details = (error.details ?? {}) as { ns?: unknown; expected?: unknown; actual?: unknown };
  return {
    ns: typeof details.ns === "string" ? details.ns : "",
    expected: typeof details.expected === "number" ? details.expected : undefined,
    actual: typeof details.actual === "number" ? details.actual : undefined,
    message: error.message,
  };
}
