// Permission preset presentation shared by both current-session pickers and the
// settings row. Source: upstream
// packages/client/ui-permission-presets/src/client/{presentation.ts,locales.ts}.

import type { PermissionOption } from "../wire";

/** Machine value of the preset that requires an explicit risk acknowledgement. */
export const FULL_ACCESS_PRESET = "danger-full-access";

/** Machine value of the experimental same-model review preset. */
export const AUTO_REVIEW_PRESET = "auto";

/** Derived value reported when knobs match no named preset; never a switch target. */
export const CUSTOM_PRESET = "custom";

const PRESET_LABELS: Record<string, string> = {
  "read-only": "仅可查看",
  "workspace-write": "工作区内修改",
  [FULL_ACCESS_PRESET]: "完全权限",
  [AUTO_REVIEW_PRESET]: "自动审查",
  [CUSTOM_PRESET]: "自定义",
};

/** Host-supplied default names that stand in for the built-in product label. */
const DEFAULT_PRESET_NAMES: Record<string, string> = {
  "read-only": "Read Only",
  "workspace-write": "Workspace Write",
  [FULL_ACCESS_PRESET]: "Full access",
  [AUTO_REVIEW_PRESET]: "Auto review",
};

/** Convert a conventional kebab-case preset name into user-facing title case. */
export function displayPresetName(name: string): string {
  if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(name)) return name;
  return name
    .split("-")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/**
 * Render a preset under its product label while preserving host copy: only the
 * built-in names (or the host echoing the machine value) take the locale label.
 */
export function displayPermissionPreset(value: string, name: string): string {
  const label = PRESET_LABELS[value];
  if (label !== undefined && (name === value || name === DEFAULT_PRESET_NAMES[value])) {
    return label;
  }
  return displayPresetName(name);
}

/** Experimental badge for the Auto review preset. */
export function presetBadge(value: string): string | undefined {
  return value === AUTO_REVIEW_PRESET ? "EXP" : undefined;
}

/** Option detail text; Auto review copy is product-owned rather than host-owned. */
export function presetDescription(option: PermissionOption): string | undefined {
  if (option.value === AUTO_REVIEW_PRESET) {
    return "无沙箱运行；每次原生工具调用和 PTC 内层调用前由同一模型进行实验性审查。";
  }
  return option.description;
}

/** Whether picking this preset must pass an explicit risk acknowledgement. */
export function requiresRiskConfirmation(value: string): boolean {
  return value === FULL_ACCESS_PRESET || value === AUTO_REVIEW_PRESET;
}

/** Where the acknowledgement runs; the settings row governs later sessions. */
export type RiskSurface = "session" | "settings";

export interface RiskCopy {
  title: string;
  description: string;
  acknowledgeLabel: string;
  cancelLabel: string;
  confirmLabel: string;
}

const FULL_ACCESS_SESSION: RiskCopy = {
  title: "确认启用完全权限？",
  description:
    "启用完全权限后，智能体将减少确认步骤，并且可以直接执行更多操作，包括敏感操作、文件修改或外部命令。仅建议在你信任当前任务时使用。",
  acknowledgeLabel: "我已了解风险，并愿意继续",
  cancelLabel: "取消",
  confirmLabel: "启用完全权限",
};

const FULL_ACCESS_SETTINGS: RiskCopy = {
  ...FULL_ACCESS_SESSION,
  description:
    "启用完全权限后，新会话将减少确认步骤，并且可以直接执行更多操作，包括敏感操作、文件修改或外部命令。仅建议在你信任后续任务时使用。",
};

const AUTO_REVIEW: RiskCopy = {
  title: "确认启用自动审查（实验）？",
  description:
    "自动审查不使用沙箱。每次原生工具调用和 PTC 内层调用前，都会由与当前 agent 相同的模型进行审查；审查拒绝的调用由你批准或拒绝。此功能仍属实验性，可能误放行或误拒绝，并会消耗额外 token。",
  acknowledgeLabel: "我已了解这些风险，并愿意继续",
  cancelLabel: "取消",
  confirmLabel: "启用自动审查",
};

/** Copy for a gated preset, or null when the value needs no acknowledgement. */
export function riskCopyFor(value: string, surface: RiskSurface): RiskCopy | null {
  if (value === AUTO_REVIEW_PRESET) return AUTO_REVIEW;
  if (value === FULL_ACCESS_PRESET) {
    return surface === "settings" ? FULL_ACCESS_SETTINGS : FULL_ACCESS_SESSION;
  }
  return null;
}
