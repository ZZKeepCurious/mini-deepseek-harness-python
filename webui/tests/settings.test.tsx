// Settings dialog, namespace form and settings/credentials wire tests.
//
// No live backend runs here: fetch is stubbed with responses whose fields are
// copied from the real web-profile probes (build/probe/out/settings-describe.json
// and fe4a-*.json) — every plugin namespace answers `schema: {}`, describe is
// redacted, and a write answers the fresh namespace view fenced by the revision
// the caller read. Requests are asserted verbatim so the wire shape (method,
// `{args}` payload, path ops, expectedRevision) is pinned.

import { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSettings } from "../src/app/useSettings";
import { usePreferences } from "../src/app/usePreferences";
import { resetPreferencesForTests } from "../src/app/uiPreferences";
import type { EmitSubscribe } from "../src/app/useBackend";
import { SettingsDialog } from "../src/ui/SettingsDialog";
import {
  RpcFailure,
  describeCredentials,
  describeSettings,
  mutateSettings,
  settingsConflictOf,
  updateSettings,
  type PermissionCatalog,
  type SettingsDescribeView,
  type SettingsNamespaceView,
  type SettingsPathOp,
} from "../src/wire";

interface WireCall {
  method: string;
  args: Record<string, unknown>;
}

function ok(rpcId: string, value?: unknown): Response {
  const result = value === undefined ? { ok: true } : { ok: true, value };
  return new Response(JSON.stringify({ type: "server-response", rpcId, result }), { status: 200 });
}

function fail(
  rpcId: string,
  error: { code: string; message: string; details?: unknown },
): Response {
  return new Response(
    JSON.stringify({ type: "server-response", rpcId, result: { ok: false, error } }),
    { status: 200 },
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function namespaceView(
  overrides: Partial<SettingsNamespaceView> & { ns: string },
): SettingsNamespaceView {
  return {
    autoGenerate: true,
    schema: {},
    value: {},
    applies: "live",
    secrets: [],
    revision: 1,
    ...overrides,
  };
}

const CATALOG: PermissionCatalog = {
  options: [
    { value: "read-only", name: "read-only" },
    { value: "workspace-write", name: "workspace-write" },
    { value: "danger-full-access", name: "danger-full-access" },
  ],
  defaultOptions: [
    { value: "read-only", name: "read-only" },
    { value: "workspace-write", name: "workspace-write" },
    { value: "danger-full-access", name: "danger-full-access" },
  ],
  defaultPreset: "workspace-write",
};

function fixtures(): SettingsNamespaceView[] {
  return [
    namespaceView({
      ns: "permission-presets",
      value: {
        presets: {
          "read-only": { sandbox: "read-only", approval: "ask" },
          "workspace-write": { sandbox: "workspace-write", approval: "ask" },
          "danger-full-access": { sandbox: "danger-full-access", approval: "never" },
        },
      },
      revision: 2,
    }),
    namespaceView({
      ns: "message-feedback",
      value: { maxNoteBytes: 8192 },
      user: { maxNoteBytes: 4096 },
      revision: 7,
    }),
    namespaceView({
      ns: "deepseek-account",
      secrets: [{ path: ["apiKey"], set: true }],
      revision: 9,
    }),
  ];
}

class FakeServer {
  calls: WireCall[] = [];
  namespaces: SettingsNamespaceView[];
  catalog: PermissionCatalog = structuredClone(CATALOG);
  conflicts = new Map<string, { expected?: number; actual: number }>();
  private readonly listeners = new Map<string, Set<(args: unknown[]) => void>>();

  constructor(namespaces: SettingsNamespaceView[]) {
    this.namespaces = namespaces;
  }

  subscribe: EmitSubscribe = (event, listener) => {
    let bucket = this.listeners.get(event);
    if (bucket === undefined) {
      bucket = new Set();
      this.listeners.set(event, bucket);
    }
    bucket.add(listener);
    return () => {
      bucket.delete(listener);
    };
  };

  emit(event: string, args: unknown[]): void {
    for (const listener of [...(this.listeners.get(event) ?? [])]) listener(args);
  }

  fetch = vi.fn(async (_url: string, init?: RequestInit): Promise<Response> => {
    const body = JSON.parse(String(init?.body)) as {
      rpcId: string;
      method: string;
      payload: { args: Record<string, unknown> };
    };
    this.calls.push({ method: body.method, args: body.payload.args });
    switch (body.method) {
      case "settings/describe":
        return ok(body.rpcId, {
          writable: true,
          hasDocument: true,
          namespaces: this.namespaces,
        });
      case "permissionPresets/catalog":
        return ok(body.rpcId, this.catalog);
      case "settings/update":
      case "settings/mutate":
        return this.write(body.rpcId, body.method, body.payload.args);
      case "settings/openSettingsDocument":
        return fail(body.rpcId, {
          code: "gateway/internal",
          message:
            "path open failed: no native desktop opener is available in this deployment",
        });
      case "credentials/describe":
        return ok(body.rpcId, { deepseek: { configured: false, writable: true } });
      default:
        return fail(body.rpcId, {
          code: "gateway/error",
          message: `unexpected method ${body.method}`,
        });
    }
  });

  private write(
    rpcId: string,
    method: string,
    args: Record<string, unknown>,
  ): Response {
    const ns = String(args.ns);
    const current = this.namespaces.find((view) => view.ns === ns);
    if (current === undefined) {
      return fail(rpcId, { code: "settings/rejected", message: ns, details: { ns } });
    }
    const conflict = this.conflicts.get(ns);
    if (conflict !== undefined) {
      this.conflicts.delete(ns);
      return fail(rpcId, {
        code: "settings/conflict",
        message: `settings revision ${String(conflict.expected)} is stale; current is ${String(conflict.actual)}`,
        details: { ns, expected: conflict.expected, actual: conflict.actual },
      });
    }

    const next = structuredClone(current);
    const secretKeys = new Set(
      next.secrets.filter((secret) => secret.path.length === 1).map((secret) => String(secret.path[0])),
    );
    const presence = new Map(
      next.secrets.map((secret) => [String(secret.path[0]), secret.set]),
    );
    const value: Record<string, unknown> = isRecord(next.value) ? { ...next.value } : {};
    const user: Record<string, unknown> = isRecord(next.user) ? { ...next.user } : {};

    if (method === "settings/update") {
      const patch = isRecord(args.patch) ? args.patch : {};
      for (const [key, entry] of Object.entries(patch)) {
        value[key] = entry;
        user[key] = entry;
        if (secretKeys.has(key)) presence.set(key, true);
      }
    } else {
      for (const op of args.ops as SettingsPathOp[]) {
        if (op.path.length !== 1) continue;
        const key = String(op.path[0]);
        if (op.op === "set") {
          value[key] = op.value;
          user[key] = op.value;
        } else {
          delete value[key];
          delete user[key];
        }
        if (secretKeys.has(key)) presence.set(key, op.op === "set");
      }
    }

    // describe is redacted: a stored secret never rides the read.
    for (const key of secretKeys) {
      delete value[key];
      delete user[key];
    }
    next.value = value;
    if (Object.keys(user).length === 0) delete next.user;
    else next.user = user;
    next.secrets = next.secrets.map((secret) =>
      secret.path.length === 1 && presence.has(String(secret.path[0]))
        ? { path: secret.path, set: presence.get(String(secret.path[0])) === true }
        : secret,
    );
    next.revision = current.revision + 1;
    this.namespaces = this.namespaces.map((view) => (view.ns === ns ? next : view));
    return ok(rpcId, next);
  }
}

function Harness({ server }: { server: FakeServer }) {
  const settings = useSettings(server.subscribe);
  const { preferences, update } = usePreferences();
  const [open, setOpen] = useState(true);
  return (
    <SettingsDialog
      open={open}
      settings={settings}
      preferences={preferences}
      onPreference={update}
      onClose={() => setOpen(false)}
      version="0.1.0"
    />
  );
}

async function openSettings(server: FakeServer): Promise<void> {
  vi.stubGlobal("fetch", server.fetch);
  render(<Harness server={server} />);
  const permission = (await screen.findByLabelText("权限")) as HTMLSelectElement;
  await waitFor(() => expect(permission.options).toHaveLength(3));
  await waitFor(() => expect(permission.value).toBe("workspace-write"));
}

function namespaceCard(ns: string): HTMLElement {
  return screen.getByRole("button", { name: new RegExp(ns, "u") });
}

beforeEach(() => {
  localStorage.clear();
  resetPreferencesForTests();
  delete document.documentElement.dataset.theme;
  document.documentElement.removeAttribute("style");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SettingsDialog shell", () => {
  it("lists only the sections this build can operate and closes on Escape", async () => {
    const server = new FakeServer(fixtures());
    await openSettings(server);

    const dialog = screen.getByRole("dialog", { name: "设置" });
    const nav = screen.getAllByRole("button").filter((button) =>
      ["常规", "内置插件", "模型", "Agent 预设"].includes(button.textContent ?? ""),
    );
    expect(nav.map((button) => button.textContent)).toEqual(["常规", "内置插件"]);
    expect(screen.getByText("当前版本：0.1.0")).toBeTruthy();
    expect(screen.getByRole("button", { name: "打开配置文件" })).toBeTruthy();

    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "设置" })).toBeNull());
  });

  it("surfaces the deployment's missing desktop opener instead of pretending it opened", async () => {
    const server = new FakeServer(fixtures());
    await openSettings(server);

    fireEvent.click(screen.getByRole("button", { name: "打开配置文件" }));
    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("no native desktop opener is available");
  });
});

describe("SettingsDialog General", () => {
  it("persists the new-session default preset and gates full access behind the risk acknowledgment", async () => {
    const server = new FakeServer(fixtures());
    await openSettings(server);
    const select = screen.getByLabelText("权限") as HTMLSelectElement;

    fireEvent.change(select, { target: { value: "read-only" } });
    await waitFor(() =>
      expect(server.calls.filter((call) => call.method === "settings/update")).toHaveLength(1),
    );
    expect(server.calls.filter((call) => call.method === "settings/update")[0].args).toEqual({
      ns: "permission-presets",
      patch: { defaultPreset: "read-only" },
      expectedRevision: 2,
    });
    await waitFor(() => expect(select.value).toBe("read-only"));

    fireEvent.change(select, { target: { value: "danger-full-access" } });
    expect(screen.getByRole("dialog", { name: "确认启用完全权限？" })).toBeTruthy();
    const confirm = screen.getByRole("button", { name: "启用完全权限" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    expect(server.calls.filter((call) => call.method === "settings/update")).toHaveLength(1);

    fireEvent.click(screen.getByRole("checkbox", { name: "我已了解风险，并愿意继续" }));
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(server.calls.filter((call) => call.method === "settings/update")).toHaveLength(2),
    );
    expect(server.calls.filter((call) => call.method === "settings/update")[1].args).toEqual({
      ns: "permission-presets",
      patch: { defaultPreset: "danger-full-access" },
      expectedRevision: 3,
    });
    await waitFor(() =>
      expect((screen.getByLabelText("权限") as HTMLSelectElement).value).toBe(
        "danger-full-access",
      ),
    );
  });

  it("re-reads the document and explains the retry after a stale revision", async () => {
    const server = new FakeServer(fixtures());
    await openSettings(server);
    const select = screen.getByLabelText("权限") as HTMLSelectElement;
    const describesBefore = server.calls.filter(
      (call) => call.method === "settings/describe",
    ).length;

    server.conflicts.set("permission-presets", { expected: 2, actual: 5 });
    fireEvent.change(select, { target: { value: "read-only" } });

    const banner = await screen.findByText((content) =>
      content.includes("配置已被其他客户端修改（revision 5）"),
    );
    expect(banner.textContent).toContain("已刷新，请重试");
    await waitFor(() =>
      expect(
        server.calls.filter((call) => call.method === "settings/describe").length,
      ).toBeGreaterThan(describesBefore),
    );
    await waitFor(() => expect(select.value).toBe("workspace-write"));
  });

  it("applies appearance, typography and send preferences immediately and persists one record", async () => {
    const server = new FakeServer(fixtures());
    await openSettings(server);

    fireEvent.click(screen.getByRole("button", { name: "浅色" }));
    expect(document.documentElement.dataset.theme).toBe("light");

    fireEvent.click(screen.getByRole("button", { name: "增加字号" }));
    expect(document.documentElement.style.getPropertyValue("--content-font-size")).toBe("15px");

    fireEvent.click(screen.getByRole("button", { name: "更多字体设置" }));
    fireEvent.click(screen.getByRole("button", { name: "增加代码字号" }));
    expect(document.documentElement.style.getPropertyValue("--code-font-size")).toBe("12px");

    const family = screen.getByLabelText("终端字体") as HTMLInputElement;
    fireEvent.change(family, { target: { value: "JetBrains Mono" } });
    fireEvent.blur(family);
    expect(document.documentElement.style.getPropertyValue("--font-family-terminal")).toBe(
      '"JetBrains Mono"',
    );

    fireEvent.change(screen.getByLabelText("忙碌时发送行为"), { target: { value: "steer" } });
    const stored = JSON.parse(localStorage.getItem("dsh.uiPrefs.v1") ?? "{}") as Record<
      string,
      unknown
    >;
    expect(stored).toMatchObject({
      theme: "light",
      fontSize: 15,
      codeFontSize: 12,
      terminalFontFamily: "JetBrains Mono",
      busyEnter: "steer",
    });

    const coding = screen.getByRole("switch", { name: "显示代码视图" });
    expect(coding.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(coding);
    expect(coding.getAttribute("aria-checked")).toBe("false");
  });
});

describe("SettingsDialog plugin namespaces", () => {
  it("renders value-typed fields and writes path ops fenced by the read revision", async () => {
    const server = new FakeServer(fixtures());
    await openSettings(server);
    fireEvent.click(screen.getByRole("button", { name: "内置插件" }));

    fireEvent.click(namespaceCard("permission-presets"));
    const presets = screen.getByLabelText("presets") as HTMLTextAreaElement;
    expect(presets.value).toContain('"read-only"');
    const edited = { "read-only": { sandbox: "read-only", approval: "never" } };
    fireEvent.change(presets, { target: { value: JSON.stringify(edited, null, 2) } });
    fireEvent.click(screen.getByRole("button", { name: "应用" }));

    await waitFor(() =>
      expect(server.calls.filter((call) => call.method === "settings/mutate")).toHaveLength(1),
    );
    expect(server.calls.filter((call) => call.method === "settings/mutate")[0].args).toEqual({
      ns: "permission-presets",
      ops: [{ op: "set", path: ["presets"], value: edited }],
      expectedRevision: 2,
    });

    fireEvent.click(namespaceCard("permission-presets"));
    fireEvent.click(namespaceCard("message-feedback"));
    const number = screen.getByLabelText("maxNoteBytes") as HTMLInputElement;
    expect(number.value).toBe("8192");
    expect(screen.getAllByText("已覆盖").length).toBeGreaterThan(0);
    fireEvent.change(number, { target: { value: "4096" } });
    fireEvent.blur(number);

    await waitFor(() =>
      expect(server.calls.filter((call) => call.method === "settings/mutate")).toHaveLength(2),
    );
    expect(server.calls.filter((call) => call.method === "settings/mutate")[1].args).toEqual({
      ns: "message-feedback",
      ops: [{ op: "set", path: ["maxNoteBytes"], value: 4096 }],
      expectedRevision: 7,
    });

    await waitFor(() =>
      expect(screen.getByLabelText("maxNoteBytes")).toHaveProperty("value", "4096"),
    );
    fireEvent.click(screen.getByRole("button", { name: "恢复默认" }));
    await waitFor(() =>
      expect(server.calls.filter((call) => call.method === "settings/mutate")).toHaveLength(3),
    );
    expect(server.calls.filter((call) => call.method === "settings/mutate")[2].args).toEqual({
      ns: "message-feedback",
      ops: [{ op: "unset", path: ["maxNoteBytes"] }],
      expectedRevision: 8,
    });
  });

  it("never receives a stored secret and clears it through an unset op", async () => {
    const server = new FakeServer(fixtures());
    await openSettings(server);
    fireEvent.click(screen.getByRole("button", { name: "内置插件" }));
    fireEvent.click(namespaceCard("deepseek-account"));

    const secret = screen.getByLabelText("apiKey") as HTMLInputElement;
    expect(secret.type).toBe("password");
    expect(secret.value).toBe("");
    expect(secret.placeholder).toBe("已设置（输入新值替换）");

    fireEvent.change(secret, { target: { value: "sk-new" } });
    fireEvent.blur(secret);
    await waitFor(() =>
      expect(server.calls.filter((call) => call.method === "settings/mutate")).toHaveLength(1),
    );
    expect(server.calls.filter((call) => call.method === "settings/mutate")[0].args).toEqual({
      ns: "deepseek-account",
      ops: [{ op: "set", path: ["apiKey"], value: "sk-new" }],
      expectedRevision: 9,
    });
    await waitFor(() =>
      expect((screen.getByLabelText("apiKey") as HTMLInputElement).value).toBe(""),
    );

    fireEvent.click(screen.getByRole("button", { name: "清除密钥" }));
    await waitFor(() =>
      expect(server.calls.filter((call) => call.method === "settings/mutate")).toHaveLength(2),
    );
    expect(server.calls.filter((call) => call.method === "settings/mutate")[1].args).toEqual({
      ns: "deepseek-account",
      ops: [{ op: "unset", path: ["apiKey"] }],
      expectedRevision: 10,
    });
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "清除密钥" }) as HTMLButtonElement).disabled,
      ).toBe(true),
    );
  });

  it("refreshes when another client reports a document update", async () => {
    const server = new FakeServer(fixtures());
    await openSettings(server);
    fireEvent.click(screen.getByRole("button", { name: "内置插件" }));
    expect(screen.getByText("r7")).toBeTruthy();
    const before = server.calls.filter((call) => call.method === "settings/describe").length;

    await act(async () => {
      server.namespaces = server.namespaces.map((view) =>
        view.ns === "message-feedback" ? { ...view, revision: 8 } : view,
      );
      server.emit("settings/document-updated", [{ ns: "message-feedback", revision: 8 }]);
    });

    await waitFor(() =>
      expect(
        server.calls.filter((call) => call.method === "settings/describe").length,
      ).toBe(before + 1),
    );
    await waitFor(() => expect(screen.getByText("r8")).toBeTruthy());
  });
});

describe("settings and credentials wire", () => {
  it("describes through settings/describe with an empty args envelope", async () => {
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as {
        rpcId: string;
        method: string;
        payload: unknown;
      };
      expect(body.method).toBe("settings/describe");
      expect(body.payload).toEqual({ args: {} });
      const view: SettingsDescribeView = {
        writable: true,
        hasDocument: true,
        namespaces: fixtures(),
      };
      return ok(body.rpcId, view);
    });
    vi.stubGlobal("fetch", fetchImpl);

    const view = await describeSettings();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(view.namespaces.map((entry) => entry.ns)).toEqual([
      "permission-presets",
      "message-feedback",
      "deepseek-account",
    ]);
    expect(view.namespaces[0].schema).toEqual({});
    expect(view.namespaces[2].secrets).toEqual([{ path: ["apiKey"], set: true }]);
  });

  it("updates with the read revision and omits it when the caller has none", async () => {
    const seen: unknown[] = [];
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as {
        rpcId: string;
        method: string;
        payload: unknown;
      };
      seen.push(body.payload);
      return ok(body.rpcId, namespaceView({ ns: "permission-presets", revision: 5 }));
    });
    vi.stubGlobal("fetch", fetchImpl);

    const view = await updateSettings(
      "permission-presets",
      { defaultPreset: "read-only" },
      4,
    );
    expect(view.revision).toBe(5);
    await updateSettings("permission-presets", { defaultPreset: "read-only" });
    expect(seen).toEqual([
      { args: { ns: "permission-presets", patch: { defaultPreset: "read-only" }, expectedRevision: 4 } },
      { args: { ns: "permission-presets", patch: { defaultPreset: "read-only" } } },
    ]);
  });

  it("mutates path ops verbatim", async () => {
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as { rpcId: string; payload: unknown };
      expect(body.payload).toEqual({
        args: {
          ns: "permission-presets",
          ops: [
            { op: "set", path: ["presets", "read-only", "approval"], value: "never" },
            { op: "unset", path: ["maxNoteBytes"] },
          ],
          expectedRevision: 2,
        },
      });
      return ok(body.rpcId, namespaceView({ ns: "permission-presets", revision: 3 }));
    });
    vi.stubGlobal("fetch", fetchImpl);

    await mutateSettings(
      "permission-presets",
      [
        { op: "set", path: ["presets", "read-only", "approval"], value: "never" },
        { op: "unset", path: ["maxNoteBytes"] },
      ],
      2,
    );
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("describes credentials without echoing the stored value", async () => {
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as { rpcId: string; payload: unknown };
      expect(body.payload).toEqual({ args: { refs: ["deepseek"] } });
      return ok(body.rpcId, { deepseek: { configured: false, writable: true } });
    });
    vi.stubGlobal("fetch", fetchImpl);

    const view = await describeCredentials(["deepseek"]);
    expect(view).toEqual({ deepseek: { configured: false, writable: true } });
  });

  it("projects settings/conflict details and ignores other refusals", () => {
    const conflict = new RpcFailure({
      code: "settings/conflict",
      message: "settings revision 2 is stale; current is 5",
      details: { ns: "permission-presets", expected: 2, actual: 5 },
    });
    expect(settingsConflictOf(conflict)).toEqual({
      ns: "permission-presets",
      expected: 2,
      actual: 5,
      message: "settings/conflict: settings revision 2 is stale; current is 5",
    });
    expect(
      settingsConflictOf(new RpcFailure({ code: "settings/rejected", message: "boom" })),
    ).toBeNull();
    expect(settingsConflictOf(new Error("boom"))).toBeNull();
  });
});
