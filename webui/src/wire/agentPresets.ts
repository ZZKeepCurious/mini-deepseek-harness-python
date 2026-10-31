import { rpc } from "./rpc";

export interface AgentPresetRow {
  id: string;
  name?: string;
  description?: string;
  isDefault?: boolean;
  broken?: boolean;
}

export interface AgentPresetList {
  presets: AgentPresetRow[];
}

export interface AgentPresetDocument {
  agentPreset: string;
  name?: string;
  description?: string;
  content: string;
}

export async function listAgentPresets(): Promise<AgentPresetList> {
  return rpc<AgentPresetList>("agentPresets/list", {});
}

export async function readAgentPreset(agentPreset: string): Promise<AgentPresetDocument> {
  return rpc<AgentPresetDocument>("agentPresets/read", { agentPreset });
}

export async function selectAgentPreset(sessionId: string, agentPreset: string): Promise<void> {
  await rpc("agentPresets/select", { sessionId, agentPreset });
}
