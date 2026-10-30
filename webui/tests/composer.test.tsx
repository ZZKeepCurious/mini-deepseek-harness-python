import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Composer } from "../src/composer/Composer";
import type { ComposerServices } from "../src/wire/composer";
import type { PermissionCatalog } from "../src/wire";

function makeServices(overrides: Partial<ComposerServices> = {}): ComposerServices {
  return {
    listCommands: vi.fn(async () => [
      { name: "plan", description: "Enter plan mode" },
      { name: "compact", description: "Compact history" },
    ]),
    listSkills: vi.fn(async () => [
      { name: "policy-user-only", description: "user-only", modelInvocable: false },
      { name: "policy-shared", description: "shared", modelInvocable: true },
    ]),
    listFileReferences: vi.fn(async () => [
      { path: "folderx", kind: "directory" as const },
      { path: "reference.txt", kind: "file" as const },
    ]),
    listSessionReferences: vi.fn(async () => [
      {
        sessionId: "s2",
        label: "Reference target",
        displayTitle: "Reference target",
        cwd: "C:/work",
        mention: "@[Reference target](dsh-session:InMy)",
      },
    ]),
    uploadFile: vi.fn(async () => ({
      receiptId: "receipt-1",
      file: { name: "note.txt" },
    })),
    executeCommand: vi.fn(async () => ({
      commandId: "cmd-1",
      result: { kind: "success" as const },
    })),
    sendPrompt: vi.fn(async () => undefined),
    ...overrides,
  };
}

function textbox(): HTMLTextAreaElement {
  return screen.getByRole("textbox", {
    name: "Message or run a task, / commands, @ files or sessions",
  }) as HTMLTextAreaElement;
}

beforeEach(() => {
  localStorage.clear();
});

describe("Composer draft, focus and cap", () => {
  it("caps the editor at fourteen lines with one scrollport", async () => {
    const services = makeServices();
    render(<Composer sessionId="s1" running={false} services={services} />);
    await waitFor(() => expect(services.listCommands).toHaveBeenCalled());
    await waitFor(() => expect(services.listSkills).toHaveBeenCalled());
    const input = textbox();
    expect(input.style.maxHeight).toBe("calc(14 * 1.5em + 20px)");
    expect(input.style.overflowY).toBe("auto");
    expect(input.rows).toBe(1);
  });

  it("rejects whitespace submission and clears the placeholder once content exists", async () => {
    const services = makeServices();
    render(<Composer sessionId="s1" running={false} services={services} />);
    await waitFor(() => expect(services.listCommands).toHaveBeenCalled());
    await waitFor(() => expect(services.listSkills).toHaveBeenCalled());
    const input = textbox();
    expect(input.placeholder).toBe("Message or run a task, / commands, @ files or sessions");
    fireEvent.change(input, { target: { value: " " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(services.sendPrompt).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: "" } });
    expect(input.value).toBe("");
  });

  it("restores the draft and selection after remount", async () => {
    const first = render(<Composer sessionId="s1" running={false} services={makeServices()} />);
    const input = textbox();
    fireEvent.change(input, { target: { value: "alpha beta gamma" } });
    fireEvent.focus(input);
    input.setSelectionRange(6, 10);
    expect(input.selectionStart).toBe(6);
    fireEvent.keyUp(input, { key: "Shift" });
    first.unmount();

    render(<Composer sessionId="s1" running={false} services={makeServices()} />);
    const restored = textbox();
    await waitFor(() => expect(restored.value).toBe("alpha beta gamma"));
    fireEvent.focus(restored);
    expect(restored.selectionStart).toBe(6);
    expect(restored.selectionEnd).toBe(10);
  });

  it("focuses and restores selection when the surrounding padding is clicked", async () => {
    const services = makeServices();
    const { container } = render(
      <Composer sessionId="s1" running={false} services={services} />,
    );
    await waitFor(() => expect(services.listCommands).toHaveBeenCalled());
    await waitFor(() => expect(services.listSkills).toHaveBeenCalled());
    const input = textbox();
    fireEvent.change(input, { target: { value: "alpha beta gamma" } });
    fireEvent.focus(input);
    input.setSelectionRange(6, 10);
    fireEvent.keyUp(input, { key: "Shift" });
    fireEvent.blur(input);
    const shell = container.querySelector(".composer");
    expect(shell).not.toBeNull();
    fireEvent.mouseDown(shell!);
    expect(document.activeElement).toBe(input);
    expect(input.selectionStart).toBe(6);
    expect(input.selectionEnd).toBe(10);
  });
});

describe("Composer slash and reference menus", () => {
  it("shows commands, inserts the selected command and executes it", async () => {
    const services = makeServices();
    render(<Composer sessionId="s1" running={false} services={services} />);
    const input = textbox();
    fireEvent.change(input, { target: { value: "/pla" } });
    expect(await screen.findByText("plan")).toBeTruthy();
    expect(screen.getByText("Commands")).toBeTruthy();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(input.value).toBe("/plan ");
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(services.executeCommand).toHaveBeenCalledWith("s1", "/plan", []),
    );
    expect(services.sendPrompt).not.toHaveBeenCalled();
  });

  it("invokes a user-only skill by sending its literal slash prompt", async () => {
    const services = makeServices();
    render(<Composer sessionId="s1" running={false} services={services} />);
    const input = textbox();
    fireEvent.change(input, { target: { value: "/policy" } });
    expect(await screen.findByText("policy-user-only")).toBeTruthy();
    expect(screen.getAllByText("user-only").length).toBeGreaterThan(0);
    expect(screen.getByText("Available to both model and user invocation")).toBeTruthy();
    expect(screen.getByText("Available only to user invocation")).toBeTruthy();
    fireEvent.click(screen.getByText("policy-user-only"));
    expect(input.value).toBe("/policy-user-only ");
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(services.sendPrompt).toHaveBeenCalledWith(
        "s1",
        [{ type: "text", text: "/policy-user-only" }],
        "queue",
      ),
    );
    expect(services.executeCommand).not.toHaveBeenCalled();
  });

  it("inserts file and session mentions from the @ menu", async () => {
    const services = makeServices();
    render(<Composer sessionId="s1" running={false} services={services} />);
    const input = textbox();
    fireEvent.change(input, { target: { value: "@ref" } });
    expect(await screen.findByText("reference.txt")).toBeTruthy();
    expect(screen.getByText("Files & folders")).toBeTruthy();
    expect(screen.getByText("Sessions")).toBeTruthy();
    fireEvent.click(screen.getByText("reference.txt"));
    expect(input.value).toBe("@reference.txt ");
    expect(services.listFileReferences).toHaveBeenCalledWith("s1", "ref");

    fireEvent.change(input, { target: { value: "@target" } });
    fireEvent.click(await screen.findByText("Reference target"));
    expect(input.value).toBe("@[Reference target](dsh-session:InMy) ");
  });

  it("quotes file mentions whose paths contain spaces", async () => {
    const services = makeServices({
      listFileReferences: vi.fn(async () => [
        { path: "meeting notes.md", kind: "file" as const },
      ]),
    });
    render(<Composer sessionId="s1" running={false} services={services} />);
    const input = textbox();
    fireEvent.change(input, { target: { value: "@meet" } });
    fireEvent.click(await screen.findByText("meeting notes.md"));
    expect(input.value).toBe('@"meeting notes.md" ');
  });

  it("keeps an unquoted directory completion open for another level", async () => {
    const services = makeServices();
    render(<Composer sessionId="s1" running={false} services={services} />);
    const input = textbox();
    fireEvent.change(input, { target: { value: "@fold" } });
    fireEvent.click(await screen.findByText("folderx/"));
    expect(input.value).toBe("@folderx/");
    await waitFor(() =>
      expect(services.listFileReferences).toHaveBeenCalledWith("s1", "folderx/"),
    );
  });
});

describe("Composer attachments and steering", () => {
  it("uploads a file and sends its receipt with the prompt", async () => {
    const services = makeServices();
    const { container } = render(
      <Composer sessionId="s1" running={false} services={services} />,
    );
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["hello"], "note.txt", { type: "text/plain" });
    fireEvent.change(fileInput, { target: { files: [file] } });
    expect(await screen.findByText("note.txt")).toBeTruthy();
    await waitFor(() => expect(services.uploadFile).toHaveBeenCalledWith("s1", file));
    const input = textbox();
    fireEvent.change(input, { target: { value: "read this" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(services.sendPrompt).toHaveBeenCalledWith(
        "s1",
        [
          { type: "text", text: "read this" },
          { type: "file", receiptId: "receipt-1" },
        ],
        "queue",
      ),
    );
  });

  it("defaults busy Enter to queue and switches to steer", async () => {
    const services = makeServices();
    render(<Composer sessionId="s1" running services={services} />);
    const input = textbox();
    fireEvent.change(input, { target: { value: "first" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(services.sendPrompt).toHaveBeenCalledWith(
        "s1",
        [{ type: "text", text: "first" }],
        "queue",
      ),
    );

    fireEvent.click(screen.getAllByRole("button", { name: "插话" })[0]);
    fireEvent.change(input, { target: { value: "second" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(services.sendPrompt).toHaveBeenLastCalledWith(
        "s1",
        [{ type: "text", text: "second" }],
        "steer",
      ),
    );
  });

  it("surfaces upload failures without sending", async () => {
    const services = makeServices({
      uploadFile: vi.fn(async () => {
        throw new Error("session/attachment-invalid: too large");
      }),
    });
    const { container } = render(
      <Composer sessionId="s1" running={false} services={services} />,
    );
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [new File(["x"], "big.bin", { type: "application/octet-stream" })] },
    });
    expect((await screen.findByRole("alert")).textContent).toContain("session/attachment-invalid");
    expect(screen.queryByText("big.bin")).toBeNull();
  });
});

const PERMISSION_CATALOG: PermissionCatalog = {
  options: [
    { value: "read-only", name: "read-only", description: "Read only files" },
    { value: "workspace-write", name: "workspace-write", description: "Write the workspace" },
    { value: "danger-full-access", name: "danger-full-access" },
    { value: "auto", name: "auto" },
  ],
  defaultOptions: [
    { value: "read-only", name: "read-only" },
    { value: "workspace-write", name: "workspace-write" },
    { value: "danger-full-access", name: "danger-full-access" },
  ],
  defaultPreset: "workspace-write",
};

function renderWithPermissions(services: ComposerServices) {
  return render(
    <Composer
      sessionId="s1"
      running={false}
      services={services}
      permission={{ currentValue: "workspace-write" }}
      permissionCatalog={PERMISSION_CATALOG}
    />,
  );
}

describe("Composer permission picker", () => {
  it("lists the catalog as the /permission argument slot and marks the active preset", async () => {
    const services = makeServices();
    renderWithPermissions(services);
    const input = textbox();
    fireEvent.change(input, { target: { value: "/permission" } });

    const listbox = await screen.findByRole("listbox", { name: "/permission 匹配项" });
    const options = within(listbox).getAllByRole("option");
    expect(options.map((option) => option.querySelector(".composer-menu-label")?.textContent)).toEqual([
      "仅可查看",
      "工作区内修改",
      "完全权限",
      "自动审查",
    ]);
    expect(options[1].getAttribute("aria-current")).toBe("true");
    expect(options[0].getAttribute("aria-current")).toBeNull();
    expect(within(listbox).getByText("EXP")).toBeTruthy();
  });

  it("runs the picked preset as a command and clears the draft", async () => {
    const services = makeServices();
    renderWithPermissions(services);
    const input = textbox();
    fireEvent.change(input, { target: { value: "/permission read" } });
    const listbox = await screen.findByRole("listbox", { name: "/permission 匹配项" });
    expect(within(listbox).getAllByRole("option")).toHaveLength(1);
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(services.executeCommand).toHaveBeenCalledWith("s1", "/permission read-only", []),
    );
    expect(input.value).toBe("");
    expect(services.sendPrompt).not.toHaveBeenCalled();
  });

  it("closes the argument slot on Escape", async () => {
    renderWithPermissions(makeServices());
    const input = textbox();
    fireEvent.change(input, { target: { value: "/permission" } });
    await screen.findByRole("listbox", { name: "/permission 匹配项" });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("listbox", { name: "/permission 匹配项" })).toBeNull();
    expect(input.value).toBe("/permission");
  });

  it("confirms full access before switching", async () => {
    const services = makeServices();
    renderWithPermissions(services);
    const input = textbox();
    fireEvent.change(input, { target: { value: "/permission danger" } });
    const listbox = await screen.findByRole("listbox", { name: "/permission 匹配项" });
    fireEvent.click(within(listbox).getByText("完全权限"));

    const dialog = screen.getByRole("dialog", { name: "确认启用完全权限？" });
    expect(services.executeCommand).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "我已了解风险，并愿意继续" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "启用完全权限" }));
    await waitFor(() =>
      expect(services.executeCommand).toHaveBeenCalledWith("s1", "/permission danger-full-access", []),
    );
    expect(input.value).toBe("");
  });

  it("switches without confirmation from the toolbar seat", async () => {
    const services = makeServices();
    renderWithPermissions(services);
    await waitFor(() => expect(services.listCommands).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "访问模式，当前：工作区内修改" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "自动审查 EXP" }));
    const dialog = screen.getByRole("dialog", { name: "确认启用自动审查（实验）？" });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "我已了解这些风险，并愿意继续" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "启用自动审查" }));
    await waitFor(() => expect(services.executeCommand).toHaveBeenCalledWith("s1", "/permission auto", []));
  });

  it("runs an argued preset line as typed, without a picker or a second confirmation", async () => {
    const services = makeServices({
      listCommands: vi.fn(async () => [
        { name: "permission", description: "Switch the session permission preset" },
      ]),
    });
    renderWithPermissions(services);
    const input = textbox();
    fireEvent.change(input, { target: { value: "/permission danger-full-access" } });
    expect(screen.queryByRole("listbox", { name: "/permission 匹配项" })).toBeNull();
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(services.executeCommand).toHaveBeenCalledWith(
        "s1",
        "/permission danger-full-access",
        [],
      ),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(input.value).toBe("");
  });

  it("omits the seat when the host exposes no permission capability", async () => {
    const services = makeServices();
    render(<Composer sessionId="s1" running={false} services={services} />);
    await waitFor(() => expect(services.listCommands).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: /^访问模式/u })).toBeNull();
  });
});
