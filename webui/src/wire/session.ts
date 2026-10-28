// `session` model-selection RPC used by the account flow: after a sign-in
// stores a DeepSeek grant, the Host picks the first available account model
// when no provider API key is configured (upstream session-controller
// `initializeDefaultModel`). The call answers nothing when a selection was
// applied and refuses with `session/provider-models-unavailable` otherwise.

import { rpc } from "./rpc";

/** Persist the default account model after a completed sign-in. */
export async function initializeDefaultModel(): Promise<void> {
  await rpc("session/initializeDefaultModel", {});
}
