import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { TerminalPanel } from "../src/ui/TerminalPanel";
import type { TerminalServices } from "../src/wire/terminal";

function services(overrides: Partial<TerminalServices> = {}): TerminalServices {
  return {
    environment: vi.fn(async () => ({
      cwd: "C:/work",
      maxInputBytes: 4096,
      maxBufferedBytes: 2 * 1024 * 1024,
      maxCols: 240,
      maxRows: 80,
      scrollback: 1000,
    })),
    shells: vi.fn(async () => [
      { path: "C:/Windows/System32/cmd.exe", name: "cmd.exe", args: [] },
      { path: "C:/Program Files/PowerShell/7/pwsh.exe", name: "pwsh.exe", args: ["-NoLogo"] },
    ]),
    list: vi.fn(async () => []),
    create: vi.fn(),
    rename: vi.fn(async () => undefined),
    close: vi.fn(async () => undefined),
    retain: vi.fn(),
    attach: vi.fn(),
    ...overrides,
  };
}

describe("TerminalPanel", () => {
  it("asks for a session before opening a shell", () => {
    render(<TerminalPanel sessionId={null} services={services()} />);
    expect(screen.getByText("选择会话后打开终端")).toBeTruthy();
  });

  it("offers installed shells in the session working directory", async () => {
    render(<TerminalPanel sessionId="s1" services={services()} />);
    expect(await screen.findByRole("button", { name: "cmd.exe" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "pwsh.exe" })).toBeTruthy();
    expect(screen.getByText("C:/work")).toBeTruthy();
  });

  it("reports a failed terminal environment and offers retry", async () => {
    const api = services({
      environment: vi.fn(async () => {
        throw new Error("terminal namespace is not mounted");
      }),
    });
    render(<TerminalPanel sessionId="s1" services={api} />);
    expect(await screen.findByText(/terminal namespace is not mounted/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });
});
