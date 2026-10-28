// General section of the settings dialog.
//
// The Permission row is host-backed: it writes `permission-presets.defaultPreset`
// through the settings wire, confirming the full-access choice first.
// Appearance/typography/coding view/busy-send rows are browser display
// preferences applied immediately (app/uiPreferences).

import { useEffect, useState } from "react";
import type { PermissionOption } from "../wire";
import { PERMISSION_SETTINGS_NS } from "../wire";
import type { UseSettings } from "../app/useSettings";
import type { UiPreferences } from "../app/uiPreferences";
import {
  CODE_FONT_SIZE_MAX,
  CODE_FONT_SIZE_MIN,
  FONT_SIZE_MAX,
  FONT_SIZE_MIN,
  TERMINAL_FONT_SIZE_MAX,
  TERMINAL_FONT_SIZE_MIN,
  THEME_PREFERENCES,
  type ThemePreference,
} from "../app/uiPreferences";

export interface SettingsGeneralProps {
  settings: UseSettings;
  preferences: UiPreferences;
  onPreference: (patch: Partial<UiPreferences>) => void;
  version: string;
}

const THEME_LABELS: Record<ThemePreference, string> = {
  light: "浅色",
  dark: "深色",
  system: "跟随系统",
};

const PRESET_LABELS: Record<string, string> = {
  "read-only": "仅可查看",
  "workspace-write": "工作区内修改",
  "danger-full-access": "完全权限",
};

const FULL_ACCESS_PRESET = "danger-full-access";

function titleCase(value: string): string {
  if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(value)) return value;
  return value
    .split("-")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function presetLabel(option: PermissionOption): string {
  return PRESET_LABELS[option.value] ?? titleCase(option.name || option.value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function currentDefaultPreset(settings: UseSettings): string {
  const view = settings.snapshot?.namespaces.find((item) => item.ns === PERMISSION_SETTINGS_NS);
  const stored = isRecord(view?.value) ? view.value.defaultPreset : undefined;
  if (typeof stored === "string" && stored !== "") return stored;
  return settings.permissionCatalog?.defaultPreset ?? "";
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, Math.round(value)));
}

function PermissionRow({ settings }: { settings: UseSettings }) {
  const [confirming, setConfirming] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const catalog = settings.permissionCatalog;
  const view = settings.snapshot?.namespaces.find((item) => item.ns === PERMISSION_SETTINGS_NS);
  const current = currentDefaultPreset(settings);
  const options = catalog?.defaultOptions ?? [];
  const writable = Boolean(settings.snapshot?.writable ?? true) && view !== undefined;
  const busy = settings.busyNs === PERMISSION_SETTINGS_NS;

  useEffect(() => {
    if (writable) return;
    setConfirming(false);
    setAcknowledged(false);
  }, [writable]);

  const write = (preset: string): void => {
    if (view === undefined) return;
    void settings.update(PERMISSION_SETTINGS_NS, { defaultPreset: preset }, view.revision);
  };

  const label = (() => {
    const option = options.find((item) => item.value === current);
    if (option !== undefined) return presetLabel(option);
    if (current !== "") return titleCase(current);
    return settings.loading ? "加载中" : "不可用";
  })();

  return (
    <>
      <div className="settings-row">
        <div className="settings-row-text">
          <div className="settings-row-title">权限</div>
          <div className="settings-row-desc">选择新会话的默认权限模式</div>
        </div>
        <div className="settings-row-control">
          <select
            className="input settings-select"
            aria-label="权限"
            value={current}
            disabled={!writable || busy || options.length === 0}
            onChange={(event) => {
              const next = event.currentTarget.value;
              if (next === current) return;
              if (next === FULL_ACCESS_PRESET) {
                setAcknowledged(false);
                setConfirming(true);
                return;
              }
              write(next);
            }}
          >
            {options.length === 0 && <option value={current}>{label}</option>}
            {options.map((option) => (
              <option key={option.value} value={option.value}>
                {presetLabel(option)}
              </option>
            ))}
          </select>
        </div>
      </div>
      {confirming && (
        <div className="settings-risk" role="alertdialog" aria-label="确认启用完全权限">
          <div className="settings-risk-title">确认启用完全权限？</div>
          <p className="settings-risk-desc">
            启用完全权限后，新会话将减少确认步骤，并且可以直接执行更多操作，包括敏感操作、文件修改或外部命令。
            仅建议在你信任后续任务时使用。
          </p>
          <label className="settings-risk-ack">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.currentTarget.checked)}
            />
            我已了解风险，并愿意继续
          </label>
          <div className="settings-risk-actions">
            <button
              type="button"
              className="btn small ghost"
              onClick={() => {
                setAcknowledged(false);
                setConfirming(false);
              }}
            >
              取消
            </button>
            <button
              type="button"
              className="btn small danger"
              disabled={!acknowledged || busy}
              onClick={() => {
                setAcknowledged(false);
                setConfirming(false);
                write(FULL_ACCESS_PRESET);
              }}
            >
              启用完全权限
            </button>
          </div>
        </div>
      )}
    </>
  );
}

interface StepperProps {
  label: string;
  description?: string;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
}

function FontSizeStepper({ label, description, value, min, max, onChange }: StepperProps) {
  return (
    <div className="settings-row">
      <div className="settings-row-text">
        <div className="settings-row-title">{label}</div>
        {description !== undefined && <div className="settings-row-desc">{description}</div>}
      </div>
      <div className="settings-row-control">
        <div className="settings-stepper">
          <span className="settings-stepper-value">{value}</span>
          <button
            type="button"
            className="settings-stepper-arrow"
            aria-label={`减小${label}`}
            disabled={value <= min}
            onClick={() => onChange(clamp(value - 1, min, max))}
          >
            −
          </button>
          <button
            type="button"
            className="settings-stepper-arrow"
            aria-label={`增加${label}`}
            disabled={value >= max}
            onClick={() => onChange(clamp(value + 1, min, max))}
          >
            +
          </button>
        </div>
        <span className="settings-unit">px</span>
      </div>
    </div>
  );
}

function FontFamilyInput({
  label,
  value,
  onCommit,
}: {
  label: string;
  value: string;
  onCommit: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = (): void => {
    if (draft !== value) onCommit(draft);
  };
  return (
    <div className="settings-row">
      <div className="settings-row-text">
        <div className="settings-row-title">{label}</div>
        <div className="settings-row-desc">逗号分隔的字族列表，留空使用内置字体</div>
      </div>
      <div className="settings-row-control">
        <input
          className="input settings-font-family"
          type="text"
          aria-label={label}
          value={draft}
          placeholder="内置字体"
          onChange={(event) => setDraft(event.currentTarget.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") commit();
          }}
        />
      </div>
    </div>
  );
}

export function SettingsGeneral({
  settings,
  preferences,
  onPreference,
  version,
}: SettingsGeneralProps) {
  const [moreFonts, setMoreFonts] = useState(false);

  return (
    <div className="settings-section">
      <PermissionRow settings={settings} />

      <div className="settings-row">
        <div className="settings-row-text">
          <div className="settings-row-title">外观</div>
        </div>
        <div className="settings-row-control settings-cubes">
          {THEME_PREFERENCES.map((preference) => (
            <button
              key={preference}
              type="button"
              className={`settings-cube${preferences.theme === preference ? " selected" : ""}`}
              aria-pressed={preferences.theme === preference}
              onClick={() => onPreference({ theme: preference })}
            >
              {THEME_LABELS[preference]}
            </button>
          ))}
        </div>
      </div>

      <div className="settings-row">
        <div className="settings-row-text">
          <div className="settings-row-title">字号</div>
          <div className="settings-row-desc">仅影响对话内容</div>
        </div>
        <div className="settings-row-control">
          <div className="settings-stepper">
            <span className="settings-stepper-value">{preferences.fontSize}</span>
            <button
              type="button"
              className="settings-stepper-arrow"
              aria-label="减小字号"
              disabled={preferences.fontSize <= FONT_SIZE_MIN}
              onClick={() =>
                onPreference({ fontSize: clamp(preferences.fontSize - 1, FONT_SIZE_MIN, FONT_SIZE_MAX) })
              }
            >
              −
            </button>
            <button
              type="button"
              className="settings-stepper-arrow"
              aria-label="增加字号"
              disabled={preferences.fontSize >= FONT_SIZE_MAX}
              onClick={() =>
                onPreference({ fontSize: clamp(preferences.fontSize + 1, FONT_SIZE_MIN, FONT_SIZE_MAX) })
              }
            >
              +
            </button>
          </div>
          <span className="settings-unit">px</span>
          <button
            type="button"
            className="settings-expander"
            aria-label="更多字体设置"
            aria-expanded={moreFonts}
            aria-controls="settings-more-fonts"
            onClick={() => setMoreFonts((open) => !open)}
          >
            {moreFonts ? "收起字体设置" : "更多字体设置"}
          </button>
        </div>
      </div>

      {moreFonts && (
        <div className="settings-subgroup" id="settings-more-fonts">
          <FontSizeStepper
            label="代码字号"
            value={preferences.codeFontSize}
            min={CODE_FONT_SIZE_MIN}
            max={CODE_FONT_SIZE_MAX}
            onChange={(value) => onPreference({ codeFontSize: value })}
          />
          <FontSizeStepper
            label="终端字号"
            value={preferences.terminalFontSize}
            min={TERMINAL_FONT_SIZE_MIN}
            max={TERMINAL_FONT_SIZE_MAX}
            onChange={(value) => onPreference({ terminalFontSize: value })}
          />
          <FontFamilyInput
            label="文本字体"
            value={preferences.textFontFamily}
            onCommit={(value) => onPreference({ textFontFamily: value })}
          />
          <FontFamilyInput
            label="代码字体"
            value={preferences.codeFontFamily}
            onCommit={(value) => onPreference({ codeFontFamily: value })}
          />
          <FontFamilyInput
            label="终端字体"
            value={preferences.terminalFontFamily}
            onCommit={(value) => onPreference({ terminalFontFamily: value })}
          />
        </div>
      )}

      <div className="settings-row">
        <div className="settings-row-text">
          <div className="settings-row-title">显示代码视图</div>
          <div className="settings-row-desc">显示轨迹、代码差异与 Agent 预设</div>
        </div>
        <div className="settings-row-control">
          <button
            type="button"
            role="switch"
            aria-checked={preferences.showCodingView}
            aria-label="显示代码视图"
            className={`switch${preferences.showCodingView ? " on" : ""}`}
            onClick={() => onPreference({ showCodingView: !preferences.showCodingView })}
          >
            <span />
          </button>
        </div>
      </div>

      <div className="settings-row">
        <div className="settings-row-text">
          <div className="settings-row-title">忙碌时发送行为</div>
          <div className="settings-row-desc">
            Agent 运行期间 Enter 与发送按钮的行为；Cmd/Ctrl+Enter 使用另一种行为
          </div>
        </div>
        <div className="settings-row-control">
          <select
            className="input settings-select"
            aria-label="忙碌时发送行为"
            value={preferences.busyEnter}
            onChange={(event) =>
              onPreference({ busyEnter: event.currentTarget.value === "steer" ? "steer" : "queue" })
            }
          >
            <option value="queue">排队</option>
            <option value="steer">插话</option>
          </select>
        </div>
      </div>

      <div className="settings-version">当前版本：{version}</div>
    </div>
  );
}
