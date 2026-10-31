import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsDialog } from "../src/ui/SettingsDialog";
import { useModelCatalog } from "../src/app/useModelCatalog";
import { useSettings } from "../src/app/useSettings";
import { usePreferences } from "../src/app/usePreferences";
function ok(rpcId: string, value?: unknown): Response {
  const result = value === undefined ? { ok: true } : { ok: true, value };
  return new Response(JSON.stringify({ type: "server-response", rpcId, result }), { status: 200 });
}

function Harness() {
  const settings = useSettings(() => () => {});
  const { preferences, update } = usePreferences();
  const modelCatalog = useModelCatalog(null);
  const [open] = useState(true);
  return (
    <SettingsDialog
      open={open}
      settings={settings}
      preferences={preferences}
      onPreference={update}
      onClose={() => {}}
      version="0.1.0"
      modelCatalog={modelCatalog}
    />
  );
}

describe("SettingsModels", () => {
  it("lists configurable providers and runs model discovery", async () => {
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit): Promise<Response> => {
      const body = JSON.parse(String(init?.body)) as {
        rpcId: string;
        method: string;
        payload: { args: Record<string, unknown> };
      };
      switch (body.method) {
        case "session/modelCatalog":
          return ok(body.rpcId, {
            default: { provider: "fake", model: "fake-model" },
            routableProviders: ["fake"],
            groups: [{ id: "fake", name: "fake", models: [{ id: "fake-model", name: "Fake" }] }],
            failures: [],
          });
        case "llm/listConfigurableProviders":
          return ok(body.rpcId, [{
            provider: "fake",
            displayName: "Fake LLM",
            settingsNs: "credentials",
            settingsPath: [],
            nativeAuth: { env: ["FAKE_LLM_API_KEY"], oauth: false },
          }]);
        case "credentials/describe":
          return ok(body.rpcId, { FAKE_LLM_API_KEY: { configured: false, writable: true } });
        case "llm/discoverModels":
          return ok(body.rpcId, [{ id: "fake-model", name: "Fake Model" }]);
        case "settings/describe":
          return ok(body.rpcId, { writable: true, hasDocument: false, namespaces: [] });
        case "permissionPresets/catalog":
          return ok(body.rpcId, {
            options: [],
            defaultOptions: [],
            defaultPreset: "read-only",
          });
        default:
          throw new Error(`unexpected ${body.method}`);
      }
    });
    vi.stubGlobal("fetch", fetchImpl);

    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "模型" }));
    await waitFor(() => expect(screen.getByText(/Fake LLM/)).toBeTruthy());
    expect(screen.getByText("FAKE_LLM_API_KEY")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重新发现" }));
    await waitFor(() => expect(screen.getByText("Fake Model")).toBeTruthy());
  });
});
