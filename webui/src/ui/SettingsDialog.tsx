// Settings dialog shell: nav rail, section routing, banners and the config
// document action. Sections land slice by slice; the nav only lists the ones
// this build can actually operate.

import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import type { UseSettings } from "../app/useSettings";
import type { UiPreferences } from "../app/uiPreferences";
import { SettingsGeneral } from "./SettingsGeneral";
import { SettingsModels } from "./SettingsModels";
import { SettingsAgentPresets } from "./SettingsAgentPresets";
import { SettingsNamespaceForm } from "./SettingsNamespaceForm";
import type { UseModelCatalog } from "../app/useModelCatalog";

export type SettingsSection = "account" | "general" | "models" | "presets" | "plugins";

export interface SettingsDialogProps {
  open: boolean;
  settings: UseSettings;
  preferences: UiPreferences;
  onPreference: (patch: Partial<UiPreferences>) => void;
  onClose: () => void;
  version: string;
  /** Account section exported by the account feature; absent while signed out. */
  accountSection?: ReactNode;
  /** Section the next open starts on; the general section when absent. */
  initialSection?: SettingsSection;
  modelCatalog?: UseModelCatalog;
}

const SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: "account", label: "账号与余额" },
  { id: "general", label: "常规" },
  { id: "models", label: "模型" },
  { id: "presets", label: "Agent 预设" },
  { id: "plugins", label: "内置插件" },
];

export function SettingsDialog({
  open,
  settings,
  preferences,
  onPreference,
  onClose,
  version,
  accountSection,
  initialSection,
  modelCatalog,
}: SettingsDialogProps) {
  const [section, setSection] = useState<SettingsSection>(initialSection ?? "general");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const signedIn = accountSection !== undefined;

  useEffect(() => {
    if (!open) return;
    setSection(initialSection ?? "general");
    closeRef.current?.focus();
  }, [initialSection, open]);

  useEffect(() => {
    if (open) return;
    setExpanded({});
  }, [open]);

  useEffect(() => {
    // Signing out removes the account section; the selected id no longer names a destination.
    if (!signedIn && section === "account") setSection("general");
  }, [section, signedIn]);

  const toggle = useCallback((ns: string) => {
    setExpanded((current) => ({ ...current, [ns]: !current[ns] }));
  }, []);

  if (!open) return null;

  const sections = signedIn ? SECTIONS : SECTIONS.filter((item) => item.id !== "account");

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
    }
  };

  const namespaces = settings.snapshot?.namespaces ?? [];

  return (
    <div
      className="settings-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="设置"
        ref={dialogRef}
        onKeyDown={onKeyDown}
      >
        <nav className="settings-nav" aria-label="设置分区">
          <div className="settings-nav-title">设置</div>
          {sections.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`settings-nav-item${section === item.id ? " active" : ""}`}
              aria-current={section === item.id ? "page" : undefined}
              onClick={() => setSection(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <div className="settings-main">
          <header className="settings-header">
            <span className="settings-header-title">
              {SECTIONS.find((item) => item.id === section)?.label}
            </span>
            <span className="spacer" />
            <button
              type="button"
              className="btn small ghost"
              disabled={settings.snapshot?.hasDocument === false}
              onClick={() => void settings.openDocument()}
            >
              打开配置文件
            </button>
            <button ref={closeRef} type="button" className="btn small ghost" onClick={onClose}>
              关闭
            </button>
          </header>

          <div className="settings-body">
            {settings.error !== null && (
              <div className="settings-banner err" role="alert">
                {settings.error}
              </div>
            )}
            {settings.conflict !== null && (
              <div className="settings-banner warn" role="alert">
                配置已被其他客户端修改（revision {String(settings.conflict.actual ?? "?")}），已刷新，请重试。
              </div>
            )}

            {section === "account" && accountSection}

            {section === "general" && (
              <SettingsGeneral
                settings={settings}
                preferences={preferences}
                onPreference={onPreference}
                version={version}
              />
            )}

            {section === "models" && modelCatalog !== undefined && (
              <SettingsModels models={modelCatalog} />
            )}

            {section === "presets" && <SettingsAgentPresets />}

            {section === "plugins" && (
              <div className="settings-section">
                <p className="settings-section-lead">
                  配置内置插件。修改即时写入 profile；密钥字段只显示是否已设置。
                </p>
                {settings.loading && <div className="empty">加载设置…</div>}
                {!settings.loading && namespaces.length === 0 && (
                  <div className="empty">该部署没有可配置的插件命名空间。</div>
                )}
                {namespaces.map((view) => {
                  const isOpen = expanded[view.ns] === true;
                  const overridden = view.user !== undefined;
                  return (
                    <div className="settings-plugin" key={view.ns}>
                      <button
                        type="button"
                        className="settings-plugin-head"
                        aria-expanded={isOpen}
                        onClick={() => toggle(view.ns)}
                      >
                        <span className="settings-plugin-name">{view.ns}</span>
                        {view.autoGenerate && <span className="settings-badge">自动生成</span>}
                        {overridden && <span className="settings-badge on">已覆盖</span>}
                        <span className="settings-plugin-revision">r{view.revision}</span>
                      </button>
                      {isOpen && (
                        <div className="settings-plugin-body">
                          <SettingsNamespaceForm
                            view={view}
                            writable={settings.snapshot?.writable ?? false}
                            busy={settings.busyNs === view.ns}
                            onMutate={settings.mutate}
                          />
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
