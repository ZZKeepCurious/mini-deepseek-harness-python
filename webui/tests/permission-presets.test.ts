// Permission preset presentation shared by the settings row and both
// current-session pickers. Expected copy mirrors the upstream locale
// dictionaries and the recorded `access-confirmation` expectations.

import { describe, expect, it } from "vitest";
import {
  AUTO_REVIEW_PRESET,
  CUSTOM_PRESET,
  FULL_ACCESS_PRESET,
  displayPermissionPreset,
  displayPresetName,
  presetBadge,
  presetDescription,
  requiresRiskConfirmation,
  riskCopyFor,
} from "../src/app/permissionPresets";

describe("displayPresetName", () => {
  it("title-cases conventional kebab names and preserves other labels", () => {
    expect(displayPresetName("team-locked")).toBe("Team Locked");
    expect(displayPresetName("read-only")).toBe("Read Only");
    expect(displayPresetName("Team Locked")).toBe("Team Locked");
    expect(displayPresetName("team_locked")).toBe("team_locked");
  });
});

describe("displayPermissionPreset", () => {
  it("uses the product label when the host echoes the machine value or the default name", () => {
    expect(displayPermissionPreset("read-only", "read-only")).toBe("仅可查看");
    expect(displayPermissionPreset("read-only", "Read Only")).toBe("仅可查看");
    expect(displayPermissionPreset("workspace-write", "workspace-write")).toBe("工作区内修改");
    expect(displayPermissionPreset("workspace-write", "Workspace Write")).toBe("工作区内修改");
    expect(displayPermissionPreset(FULL_ACCESS_PRESET, "Full access")).toBe("完全权限");
    expect(displayPermissionPreset(AUTO_REVIEW_PRESET, "auto")).toBe("自动审查");
    expect(displayPermissionPreset(CUSTOM_PRESET, "custom")).toBe("自定义");
  });

  it("keeps a host-defined label instead of the product label", () => {
    expect(displayPermissionPreset("read-only", "Audit only")).toBe("Audit only");
    expect(displayPermissionPreset("workspace-write", "Team Sandbox")).toBe("Team Sandbox");
    expect(displayPermissionPreset("team-locked", "team-locked")).toBe("Team Locked");
  });
});

describe("preset decoration", () => {
  it("marks only the experimental review preset", () => {
    expect(presetBadge(AUTO_REVIEW_PRESET)).toBe("EXP");
    expect(presetBadge("read-only")).toBeUndefined();
    expect(presetBadge(FULL_ACCESS_PRESET)).toBeUndefined();
  });

  it("owns the Auto review explanation instead of trusting host copy", () => {
    expect(presetDescription({ value: AUTO_REVIEW_PRESET, name: "auto" })).toBe(
      "无沙箱运行；每次原生工具调用和 PTC 内层调用前由同一模型进行实验性审查。",
    );
    expect(
      presetDescription({ value: "read-only", name: "Read Only", description: "Read only files" }),
    ).toBe("Read only files");
    expect(presetDescription({ value: "workspace-write", name: "Workspace Write" })).toBeUndefined();
  });
});

describe("risk gate", () => {
  it("gates full access and experimental review only", () => {
    expect(requiresRiskConfirmation(FULL_ACCESS_PRESET)).toBe(true);
    expect(requiresRiskConfirmation(AUTO_REVIEW_PRESET)).toBe(true);
    expect(requiresRiskConfirmation("read-only")).toBe(false);
    expect(requiresRiskConfirmation("workspace-write")).toBe(false);
    expect(requiresRiskConfirmation(CUSTOM_PRESET)).toBe(false);
  });

  it("distinguishes the current-session and new-session full-access copy", () => {
    const session = riskCopyFor(FULL_ACCESS_PRESET, "session");
    const settings = riskCopyFor(FULL_ACCESS_PRESET, "settings");
    expect(session).toMatchObject({
      title: "确认启用完全权限？",
      acknowledgeLabel: "我已了解风险，并愿意继续",
      cancelLabel: "取消",
      confirmLabel: "启用完全权限",
    });
    expect(session?.description).toContain("仅建议在你信任当前任务时使用");
    expect(settings?.description).toContain("新会话将减少确认步骤");
    expect(settings?.description).toContain("仅建议在你信任后续任务时使用");
    expect(settings?.title).toBe(session?.title);
  });

  it("describes the experimental review risks", () => {
    const copy = riskCopyFor(AUTO_REVIEW_PRESET, "session");
    expect(copy).toMatchObject({
      title: "确认启用自动审查（实验）？",
      acknowledgeLabel: "我已了解这些风险，并愿意继续",
      confirmLabel: "启用自动审查",
    });
    expect(copy?.description).toContain("自动审查不使用沙箱");
    expect(copy?.description).toContain("可能误放行或误拒绝");
    expect(copy?.description).toContain("消耗额外 token");
  });

  it("returns no copy for presets that need no acknowledgement", () => {
    expect(riskCopyFor("read-only", "session")).toBeNull();
    expect(riskCopyFor("workspace-write", "settings")).toBeNull();
    expect(riskCopyFor(CUSTOM_PRESET, "session")).toBeNull();
  });
});
