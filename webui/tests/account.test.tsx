// Account frame (src/app/useAccount.ts, src/ui/AccountMenu.tsx,
// src/ui/AccountSection.tsx): the `account/watch` projection, the profile and
// balance reads it triggers, the bonus notice lifecycle, and the explicit
// sign-in/sign-out operations.
//
// No live backend runs here: the mux stream is a driven handle whose first item
// is the Host projection, and fetch answers the account RPCs with the real wire
// shapes. Requests are asserted verbatim so the metadata and attempt ids stay
// pinned.

import { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EmitSubscribe } from "../src/app/useBackend";
import { useAccount } from "../src/app/useAccount";
import type { AccountBonusBatch, AccountView, MuxServerFrame, StreamHandle } from "../src/wire";
import { AccountMenu } from "../src/ui/AccountMenu";
import { AccountSection } from "../src/ui/AccountSection";

interface WireCall {
  method: string;
  args: Record<string, unknown>;
}

function ok(rpcId: string, value?: unknown): Response {
  const result = value === undefined ? { ok: true } : { ok: true, value };
  return new Response(JSON.stringify({ type: "server-response", rpcId, result }), { status: 200 });
}

function refused(rpcId: string, code: string, message: string): Response {
  return new Response(
    JSON.stringify({ type: "server-response", rpcId, result: { ok: false, error: { code, message } } }),
    { status: 200 },
  );
}

class FakeStream implements StreamHandle {
  readonly streamId = "stream-account";
  closed = false;
  private queue: MuxServerFrame[] = [];
  private waiters: ((frame: MuxServerFrame) => void)[] = [];

  send(): void {}
  endUplink(): void {}
  close = (): void => {
    this.closed = true;
  };
  next = (): Promise<MuxServerFrame> => {
    const frame = this.queue.shift();
    if (frame !== undefined) return Promise.resolve(frame);
    return new Promise<MuxServerFrame>((resolve) => {
      this.waiters.push(resolve);
    });
  };

  push(value: unknown): void {
    const frame: MuxServerFrame = { type: "item", streamId: this.streamId, value };
    const waiter = this.waiters.shift();
    if (waiter !== undefined) waiter(frame);
    else this.queue.push(frame);
  }
}

const SIGNED_OUT: AccountView = {
  status: "signed-out",
  links: { usageUrl: "https://platform.example/usage", topUpUrl: "https://platform.example/top-up" },
  attempt: null,
};

const SIGNED_IN: AccountView = {
  status: "credential-stored",
  links: { usageUrl: "https://platform.example/usage", topUpUrl: "https://platform.example/top-up" },
  attempt: null,
};

class AccountServer {
  calls: WireCall[] = [];
  opened: { endpoint: string; payload: unknown }[] = [];
  streams: FakeStream[] = [];
  bonuses: AccountBonusBatch | null = null;
  runningTasks: boolean | "refuse" = false;
  /** Starting a sign-in while signed out keeps that status and adds the live attempt. */
  startView: AccountView = {
    ...SIGNED_OUT,
    attempt: {
      id: "attempt-1",
      phase: "waiting-browser",
      authorizeUrl: "https://platform.example/authorize?challenge=abc",
    },
  };
  private readonly listeners = new Map<string, Set<(args: unknown[]) => void>>();

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

  openStream = (endpoint: string, payload: unknown): StreamHandle => {
    this.opened.push({ endpoint, payload });
    const stream = new FakeStream();
    this.streams.push(stream);
    return stream;
  };

  push(view: AccountView): void {
    const stream = this.streams[this.streams.length - 1];
    if (stream === undefined) throw new Error("account/watch was not opened");
    act(() => {
      stream.push(view);
    });
  }

  fetch = vi.fn(async (_url: string, init?: RequestInit): Promise<Response> => {
    const body = JSON.parse(String(init?.body)) as {
      rpcId: string;
      method: string;
      payload: { args: Record<string, unknown> };
    };
    this.calls.push({ method: body.method, args: body.payload.args });
    switch (body.method) {
      case "account/getProfile":
        return ok(body.rpcId, {
          status: "ready",
          value: { id: "user-1", name: "Ada", contact: "ada@example.com", avatarUrl: "" },
        });
      case "account/getBalance":
        return ok(body.rpcId, {
          status: "ready",
          value: [{ currency: "CNY", balance: "1234.5" }],
          bonusWallets: [
            { currency: "CNY", balance: "5" },
            { currency: "USD", balance: "0" },
          ],
        });
      case "account/getUnnotifiedBonuses":
        return ok(body.rpcId, this.bonuses);
      case "account/ackBonusNotified":
        return ok(body.rpcId, true);
      case "account/startSignIn":
        return ok(body.rpcId, this.startView);
      case "account/cancelSignIn":
        return ok(body.rpcId, {
          ...SIGNED_OUT,
          attempt: { id: String(body.payload.args.attemptId), phase: "cancelled" },
        });
      case "account/signOut":
        return ok(body.rpcId, SIGNED_OUT);
      case "account/hasRunningAccountTasks":
        return this.runningTasks === "refuse"
          ? refused(body.rpcId, "gateway/disconnected", "account state unavailable")
          : ok(body.rpcId, this.runningTasks);
      case "session/initializeDefaultModel":
        return ok(body.rpcId);
      default:
        return refused(body.rpcId, "gateway/error", `unexpected method ${body.method}`);
    }
  });
}

function Harness({ server, withSection = false }: { server: AccountServer; withSection?: boolean }) {
  const account = useAccount({
    openStream: server.openStream,
    subscribe: server.subscribe,
    version: "0.1.0",
    colorScheme: "dark",
  });
  const [settingsOpen, setSettingsOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setSettingsOpen(true)}>
        设置入口
      </button>
      <AccountMenu
        account={account}
        colorScheme="dark"
        settingsOpen={settingsOpen}
        onOpenSettings={() => setSettingsOpen(true)}
        onAddApiKey={() => setSettingsOpen(true)}
      />
      {withSection && <AccountSection account={account} colorScheme="dark" />}
    </div>
  );
}

function renderAccount(server: AccountServer, options: { withSection?: boolean } = {}) {
  vi.stubGlobal("fetch", server.fetch);
  render(<Harness server={server} withSection={options.withSection === true} />);
}

async function signedIn(server: AccountServer): Promise<void> {
  server.push(SIGNED_IN);
  await screen.findAllByText("Ada");
}

function openMenu(): void {
  fireEvent.click(screen.getByRole("button", { name: "账号菜单" }));
}

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("account stream and settings", () => {
  it("opens the watch stream once and reads the profile and balance it names", async () => {
    const server = new AccountServer();
    renderAccount(server, { withSection: true });

    expect(server.opened).toEqual([{ endpoint: "account/watch", payload: { args: {} } }]);
    expect(server.calls).toEqual([]);

    await signedIn(server);

    const detailCalls = server.calls.filter(
      (call) => call.method === "account/getProfile" || call.method === "account/getBalance",
    );
    expect(detailCalls.map((call) => call.method)).toEqual([
      "account/getProfile",
      "account/getBalance",
    ]);
    const client = expect.objectContaining({
      version: "0.1.0",
      locale: expect.any(String),
      timezoneOffsetSeconds: expect.any(Number),
    });
    expect(detailCalls[0]?.args).toEqual({ client });
    expect(
      server.calls.find((call) => call.method === "account/getUnnotifiedBonuses")?.args,
    ).toEqual({ client });

    expect(screen.getByText("¥1,234.50")).toBeTruthy();
    expect(screen.getByText("¥5.00")).toBeTruthy();
    expect(screen.getByText("ada@example.com")).toBeTruthy();
    expect(screen.getByText("更多账号信息")).toBeTruthy();
    expect(screen.getByRole("link", { name: "查询用量" }).getAttribute("href")).toBe(
      "https://platform.example/usage",
    );
    expect(screen.getByRole("link", { name: "充值" }).getAttribute("href")).toBe(
      "https://platform.example/top-up",
    );
  });

  it("labels the signed-out launcher and opens the sign-in dialog", async () => {
    const popup = {
      opener: {} as unknown,
      closed: false,
      close: vi.fn(),
      location: { replace: vi.fn() },
    };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    const server = new AccountServer();
    renderAccount(server);
    server.push(SIGNED_OUT);
    await waitFor(() => expect(screen.getByRole("button", { name: "账号菜单" })).toBeTruthy());

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "登录" }));
    const dialog = await screen.findByRole("dialog", { name: "等待登录" });
    expect(within(dialog).getByText(/没有自动打开新页面/)).toBeTruthy();

    const call = server.calls.find((entry) => entry.method === "account/startSignIn");
    expect(call?.args).toEqual({
      client: expect.objectContaining({ version: "0.1.0" }),
      callbackOrigin: window.location.origin,
      loginSource: "web",
    });
  });
});

describe("account sign-in and sign-out", () => {
  it("follows the attempt in a browser tab this UI owns and cancels by its id", async () => {
    const popup = {
      opener: {} as unknown,
      closed: false,
      close: vi.fn(),
      location: { replace: vi.fn() },
    };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    const server = new AccountServer();
    renderAccount(server);
    server.push(SIGNED_OUT);
    await waitFor(() => expect(screen.getByRole("button", { name: "账号菜单" })).toBeTruthy());

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "登录" }));
    const dialog = await screen.findByRole("dialog", { name: "等待登录" });

    expect(window.open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(popup.opener).toBeNull();
    expect(popup.location.replace).toHaveBeenCalledWith(
      "https://platform.example/authorize?challenge=abc&theme=dark",
    );

    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() =>
      expect(server.calls.some((entry) => entry.method === "account/cancelSignIn")).toBe(true),
    );
    expect(
      server.calls.find((entry) => entry.method === "account/cancelSignIn")?.args,
    ).toEqual({ attemptId: "attempt-1" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "等待登录" })).toBeNull());
  });

  it("warns that signing out interrupts running tasks before it signs out", async () => {
    const server = new AccountServer();
    server.runningTasks = true;
    renderAccount(server);
    await signedIn(server);

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "退出登录" }));
    const dialog = await screen.findByRole("dialog", { name: "退出登录" });
    expect(within(dialog).getByText(/当前有进程正在执行中/)).toBeTruthy();
    expect(server.calls.find((entry) => entry.method === "account/hasRunningAccountTasks")?.args).toEqual(
      {},
    );

    fireEvent.click(within(dialog).getByRole("button", { name: "退出登录" }));
    await waitFor(() =>
      expect(server.calls.some((entry) => entry.method === "account/signOut")).toBe(true),
    );
    expect(server.calls.find((entry) => entry.method === "account/signOut")?.args).toEqual({
      client: expect.objectContaining({ version: "0.1.0" }),
    });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "退出登录" })).toBeNull());
  });

  it("reports an unverifiable task state instead of pretending there are no tasks", async () => {
    const server = new AccountServer();
    server.runningTasks = "refuse";
    renderAccount(server);
    await signedIn(server);

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "退出登录" }));
    const dialog = await screen.findByRole("dialog", { name: "退出登录" });
    expect(within(dialog).getByText(/暂时无法确认任务状态/)).toBeTruthy();
  });
});

describe("account events and bonus notices", () => {
  it("raises a toast for a credential expiry and for model sign-in guidance", async () => {
    const server = new AccountServer();
    renderAccount(server);
    await signedIn(server);

    await act(async () => {
      server.emit("deepseek-account/session-expired", []);
    });
    expect(await screen.findByText("登录信息已失效，请重新登录")).toBeTruthy();

    await act(async () => {
      server.emit("deepseek-account/model-sign-in-required", []);
    });
    expect(await screen.findByText("当前模型暂不可用，请登录后再试")).toBeTruthy();
  });

  it("shows the server-authored bonus card and acknowledges it after a presented frame", async () => {
    const server = new AccountServer();
    server.bonuses = {
      accountId: "acct-1",
      bonuses: [{ orderId: "order-1", message: "赠金 ¥10 已到账", expiresAt: "" }],
    };
    renderAccount(server);
    await signedIn(server);

    expect(await screen.findByText("赠金 ¥10 已到账")).toBeTruthy();
    expect(screen.getByText("赠金已到账")).toBeTruthy();
    await waitFor(() =>
      expect(server.calls.some((entry) => entry.method === "account/ackBonusNotified")).toBe(true),
    );
    expect(
      server.calls.find((entry) => entry.method === "account/ackBonusNotified")?.args,
    ).toEqual({
      accountId: "acct-1",
      orderId: "order-1",
      client: expect.objectContaining({ version: "0.1.0" }),
    });

    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(screen.queryByText("赠金 ¥10 已到账")).toBeNull());
  });

  it("does not draw an award that expired before its render", async () => {
    const server = new AccountServer();
    server.bonuses = {
      accountId: "acct-1",
      bonuses: [{ orderId: "order-old", message: "过期的赠金", expiresAt: "2020-01-01T00:00:00Z" }],
    };
    renderAccount(server);
    await signedIn(server);
    await waitFor(() =>
      expect(server.calls.some((entry) => entry.method === "account/getUnnotifiedBonuses")).toBe(true),
    );
    expect(screen.queryByText("过期的赠金")).toBeNull();
  });
});
