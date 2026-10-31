import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsDialog } from "../src/ui/SettingsDialog";
import { useSettings } from "../src/app/useSettings";
import { usePreferences } from "../src/app/usePreferences";

function ok(rpcId: string, value?: unknown): Response {
  const result = value === undefined ? { ok: true } : { ok: true, value };
  return new Response(JSON.stringify({ type: "server-response", rpcId, result }), { status: 200 });
}

function Harness() {
  const settings = useSettings(() => () => {});
  const { preferences, update } = usePreferences();
  const [open] = useState(true);
  return (
    <SettingsDialog
      open={open}
      settings={settings}
      preferences={preferences}
      onPreference={update}
      onClose={() => {}}
      version="0.1.0"
    />
  );
}

describe("SettingsAgentPresets", () => {
  it("lists presets and loads document content on expand", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit): Promise<Response> => {
        const body = JSON.parse(String(init?.body)) as {
          rpcId: string;
          method: string;
        };
        switch (body.method) {
          case "settings/describe":
            return ok(body.rpcId, { writable: true, hasDocument: false, namespaces: [] });
          case "permissionPresets/catalog":
            return ok(body.rpcId, { options: [], defaultOptions: [], defaultPreset: "read-only" });
          case "agentPresets/list":
            return ok(body.rpcId, {
              presets: [{ id: "default", name: "Default", isDefault: true }],
            });
          case "agentPresets/read":
            return ok(body.rpcId, {
              agentPreset: "default",
              content: "rows: []\n",
            });
          default:
            throw new Error(body.method);
        }
      }),
    );

    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Agent 预设" }));
    await waitFor(() => expect(screen.getByText("Default")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Default 默认" }));
    await waitFor(() => expect(screen.getByText("rows: []")).toBeTruthy());
  });
});
