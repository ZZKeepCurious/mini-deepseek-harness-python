import { rpc } from "./rpc";
import type { ModelBadge } from "./modelBadge";

export interface ConfigurableProvider {
  provider: string;
  displayName: string;
  settingsNs: string;
  settingsPath: string[];
  nativeAuth?: { env: readonly string[]; oauth: boolean };
}

export interface DiscoveredModel {
  id: string;
  name?: string;
  inputModalities?: string[];
  badge?: ModelBadge;
}

export async function listConfigurableProviders(): Promise<ConfigurableProvider[]> {
  return rpc<ConfigurableProvider[]>("llm/listConfigurableProviders", {});
}

export async function discoverModels(
  settingsNs: string,
  request: { provider?: string; baseURL?: string; api?: string; apiKey?: string },
): Promise<DiscoveredModel[]> {
  return rpc<DiscoveredModel[]>("llm/discoverModels", { settingsNs, request });
}
