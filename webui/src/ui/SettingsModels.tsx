import { useCallback, useEffect, useMemo, useState } from "react";
import {
  describeCredentials,
  setCredential,
  unsetCredential,
  type CredentialsDescribe,
} from "../wire/credentials";
import {
  discoverModels,
  listConfigurableProviders,
  type ConfigurableProvider,
  type DiscoveredModel,
} from "../wire/llm";
import { modelBadgeLabel } from "../wire/modelBadge";
import type { UseModelCatalog } from "../app/useModelCatalog";
import { SecretMask } from "./SecretMask";

interface Props {
  models: UseModelCatalog;
}

export function SettingsModels({ models }: Props) {
  const [providers, setProviders] = useState<ConfigurableProvider[]>([]);
  const [providerError, setProviderError] = useState<string | null>(null);
  const [credentials, setCredentials] = useState<CredentialsDescribe>({});
  const [discoverError, setDiscoverError] = useState<string | null>(null);
  const [discovering, setDiscovering] = useState<string | null>(null);
  const [discovered, setDiscovered] = useState<Record<string, DiscoveredModel[]>>({});

  const credentialRefs = useMemo(() => {
    const refs = new Set<string>();
    for (const row of providers) {
      for (const env of row.nativeAuth?.env ?? []) {
        if (env !== "") refs.add(env);
      }
    }
    return [...refs];
  }, [providers]);

  const refreshCredentials = useCallback(async () => {
    if (credentialRefs.length === 0) {
      setCredentials({});
      return;
    }
    setCredentials(await describeCredentials(credentialRefs));
  }, [credentialRefs]);

  useEffect(() => {
    let cancelled = false;
    void listConfigurableProviders()
      .then((rows) => {
        if (!cancelled) {
          setProviders(rows);
          setProviderError(null);
        }
      })
      .catch((caught) => {
        if (!cancelled) {
          setProviders([]);
          setProviderError(caught instanceof Error ? caught.message : String(caught));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    void refreshCredentials().catch((caught: unknown) => {
      setProviderError(caught instanceof Error ? caught.message : String(caught));
    });
  }, [refreshCredentials]);

  const runDiscover = useCallback(async (provider: ConfigurableProvider) => {
    setDiscovering(provider.provider);
    setDiscoverError(null);
    try {
      const rows = await discoverModels(provider.settingsNs, { provider: provider.provider });
      setDiscovered((current) => ({
        ...current,
        [provider.provider]: rows,
      }));
      await models.refresh();
    } catch (caught) {
      setDiscoverError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setDiscovering(null);
    }
  }, [models]);

  const catalog = models.catalog;
  const defaultLabel =
    models.selection === null
      ? "—"
      : `${models.selection.provider}/${models.selection.model}`;

  return (
    <div className="settings-models">
      <p className="settings-lead">
        默认模型：<strong>{defaultLabel}</strong>
      </p>
      {models.error !== null && (
        <div className="settings-banner error" role="alert">
          {models.error}
        </div>
      )}
      {providerError !== null && (
        <div className="settings-banner error" role="alert">
          {providerError}
        </div>
      )}
      <button type="button" className="btn small" disabled={models.loading} onClick={() => void models.refresh()}>
        刷新目录
      </button>
      {catalog?.groups.map((group) => (
        <section key={group.id} className="settings-models-group">
          <h3>{group.name}</h3>
          <ul>
            {group.models.map((model) => {
              const badge = modelBadgeLabel(model.badge);
              return (
                <li key={model.id}>
                  {model.name} <span className="dim">({model.id})</span>
                  {badge !== undefined && (
                    <span className="settings-badge">{badge}</span>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
      {providers.length > 0 && (
        <section className="settings-models-providers">
          <h3>Provider 凭据</h3>
          {credentialRefs.map((ref) => {
            const view = credentials[ref];
            return (
              <SecretMask
                key={ref}
                label={ref}
                configured={view?.configured === true}
                writable={view?.writable !== false}
                onSet={(value) => {
                  void setCredential(ref, value).then(() => refreshCredentials());
                }}
                onClear={() => {
                  void unsetCredential(ref).then(() => refreshCredentials());
                }}
              />
            );
          })}
        </section>
      )}
      <section className="settings-models-discovery">
        <h3>模型发现</h3>
        <p className="dim">按 provider 重新拉取目录；失败时保留上次结果。</p>
        {providers.map((provider) => (
          <div key={provider.provider} className="settings-models-discovery-row">
            <span>{provider.displayName}</span>
            <button
              type="button"
              className="btn small"
              disabled={discovering !== null}
              onClick={() => void runDiscover(provider)}
            >
              {discovering === provider.provider ? "发现中…" : "重新发现"}
            </button>
            {(discovered[provider.provider] ?? []).length > 0 && (
              <ul>
                {discovered[provider.provider].map((row) => {
                  const badge = modelBadgeLabel(row.badge);
                  return (
                    <li key={row.id}>
                      {row.name ?? row.id}
                      {badge !== undefined && (
                        <span className="settings-badge">{badge}</span>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        ))}
        {providers.length === 0 && (
          <p className="dim">当前部署未声明可配置 provider。</p>
        )}
        {discoverError !== null && (
          <div className="settings-banner error" role="alert">
            {discoverError}
          </div>
        )}
      </section>
    </div>
  );
}
