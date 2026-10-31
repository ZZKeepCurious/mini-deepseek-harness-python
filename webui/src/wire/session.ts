// `session` model-selection RPC used by the account flow: after a sign-in
// stores a DeepSeek grant, the Host picks the first available account model
// when no provider API key is configured (upstream session-controller
// `initializeDefaultModel`). The call answers nothing when a selection was
// applied and refuses with `session/provider-models-unavailable` otherwise.

import { rpc } from "./rpc";
import type { ModelBadge } from "./modelBadge";

export interface ModelSelection {
  provider: string;
  model: string;
  reasoningEffort?: string;
}

export interface ModelCatalogGroup {
  id: string;
  name: string;
  models: Array<{ id: string; name: string; badge?: ModelBadge }>;
}

export interface ModelCatalog {
  default: ModelSelection;
  routableProviders: string[];
  groups: ModelCatalogGroup[];
  failures: unknown[];
}

/** Deployment model directory and current default selection. */
export async function fetchModelCatalog(): Promise<ModelCatalog> {
  return rpc<ModelCatalog>("session/modelCatalog", {});
}

/** Install a session-scoped model choice (also persists deployment default when configured). */
export async function selectSessionModel(
  sessionId: string,
  provider: string,
  model: string,
  reasoningEffort?: string,
): Promise<{ selected: ModelSelection }> {
  return rpc("session/selectModel", {
    sessionId,
    provider,
    model,
    ...(reasoningEffort === undefined ? {} : { reasoningEffort }),
  });
}

/** Persist the default account model after a completed sign-in. */
export async function initializeDefaultModel(): Promise<void> {
  await rpc("session/initializeDefaultModel", {});
}
