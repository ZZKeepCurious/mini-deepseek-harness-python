import { useCallback, useEffect, useState } from "react";
import {
  listAgentPresets,
  readAgentPreset,
  type AgentPresetDocument,
  type AgentPresetRow,
} from "../wire/agentPresets";

export function SettingsAgentPresets() {
  const [presets, setPresets] = useState<AgentPresetRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [documents, setDocuments] = useState<Record<string, AgentPresetDocument>>({});

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listAgentPresets();
      setPresets(result.presets ?? []);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPresets([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const toggle = useCallback(
    async (id: string) => {
      if (expanded === id) {
        setExpanded(null);
        return;
      }
      setExpanded(id);
      if (documents[id] !== undefined) return;
      try {
        const doc = await readAgentPreset(id);
        setDocuments((current) => ({ ...current, [id]: doc }));
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    },
    [documents, expanded],
  );

  return (
    <div className="settings-agent-presets">
      <p className="settings-lead">部署内可用的 Agent 组合；会话首回合开始前可在 RPC 面选定。</p>
      {error !== null && (
        <div className="settings-banner error" role="alert">
          {error}
        </div>
      )}
      <button type="button" className="btn small" disabled={loading} onClick={() => void refresh()}>
        刷新列表
      </button>
      {loading && presets.length === 0 && <div className="empty">加载中…</div>}
      {!loading && presets.length === 0 && (
        <div className="empty">该部署未挂载 agent preset 注册表。</div>
      )}
      <ul className="settings-agent-preset-list">
        {presets.map((preset) => {
          const open = expanded === preset.id;
          const doc = documents[preset.id];
          return (
            <li key={preset.id} className="settings-agent-preset-item">
              <button
                type="button"
                className="settings-agent-preset-head"
                aria-expanded={open}
                onClick={() => void toggle(preset.id)}
              >
                <span>{preset.name ?? preset.id}</span>
                {preset.isDefault === true && <span className="settings-badge">默认</span>}
                {preset.broken === true && <span className="settings-badge warn">损坏</span>}
              </button>
              {preset.description !== undefined && preset.description !== "" && (
                <p className="dim">{preset.description}</p>
              )}
              {open && doc !== undefined && (
                <pre className="settings-agent-preset-doc">{doc.content}</pre>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
