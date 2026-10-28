import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DocumentPreview } from "../src/ui/DocumentPreview";
import { FilesPanel } from "../src/ui/FilesPanel";
import type { MuxServerFrame, StreamHandle } from "../src/wire/mux";
import type { WorkspaceServices } from "../src/wire/workspace";

interface FakeStream extends StreamHandle {
  push: (frame: MuxServerFrame) => void;
  closed: boolean;
}

function fakeStream(): FakeStream {
  const queue: MuxServerFrame[] = [];
  const waiters: Array<(frame: MuxServerFrame) => void> = [];
  return {
    streamId: "watch-1",
    closed: false,
    send: () => undefined,
    endUplink: () => undefined,
    close() {
      this.closed = true;
    },
    next() {
      if (queue.length > 0) return Promise.resolve(queue.shift()!);
      return new Promise((resolve) => waiters.push(resolve));
    },
    push(frame) {
      if (waiters.length > 0) waiters.shift()!(frame);
      else queue.push(frame);
    },
  };
}

function services(watches: FakeStream[] = []): WorkspaceServices {
  return {
    list: vi.fn(async (_sessionId: string, path: string) => {
      if (path === ".") {
        return {
          path: ".",
          entries: [
            { name: "src", type: "directory" },
            { name: "README.md", type: "file", size: 10 },
          ],
          truncated: false,
        };
      }
      return { path, entries: [{ name: "a.ts", type: "file", size: 3 }], truncated: false };
    }),
    stat: vi.fn(async () => ({ absolutePath: "C:/work/a.png", bytes: 5 })),
    read: vi.fn(async () => ({
      absolutePath: "C:/work/README.md",
      offset: 1,
      text: "# Hello",
      lines: 1,
      eof: true,
    })),
    watch: vi.fn(() => {
      const stream = fakeStream();
      watches.push(stream);
      return stream;
    }),
  };
}

describe("FilesPanel", () => {
  it("loads the root, expands directories and opens files", async () => {
    const onOpen = vi.fn();
    const api = services();
    render(<FilesPanel sessionId="s1" services={api} onOpen={onOpen} />);
    fireEvent.click(await screen.findByRole("treeitem", { name: /src/ }));
    expect(await screen.findByRole("treeitem", { name: /a\.ts/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("treeitem", { name: /README\.md/ }));
    expect(onOpen).toHaveBeenCalledWith("README.md");
  });

  it("shows the empty state without a session", () => {
    render(<FilesPanel sessionId={null} services={services()} onOpen={() => undefined} />);
    expect(screen.getByText("选择会话查看工作区文件")).toBeTruthy();
  });

  it("re-lists a watched directory when the backend reports a change", async () => {
    const watches: FakeStream[] = [];
    const api = services(watches);
    render(<FilesPanel sessionId="s1" services={api} onOpen={() => undefined} />);
    expect(await screen.findByRole("treeitem", { name: /README\.md/ })).toBeTruthy();
    await waitFor(() => expect(watches.length).toBeGreaterThan(0));
    (api.list as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      path: ".",
      entries: [
        { name: "src", type: "directory" },
        { name: "README.md", type: "file", size: 10 },
        { name: "NEW.md", type: "file", size: 7 },
      ],
      truncated: false,
    });
    for (const stream of watches) {
      stream.push({
        type: "item",
        streamId: "watch-1",
        value: { kind: "change", change: { absolutePath: "C:/work/NEW.md", version: "v2" } },
      });
    }
    expect(await screen.findByRole("treeitem", { name: /NEW\.md/ })).toBeTruthy();
  });
});

describe("DocumentPreview", () => {
  it("renders markdown files", async () => {
    render(
      <DocumentPreview sessionId="s1" path="README.md" services={services()} onClose={() => undefined} />,
    );
    expect(await screen.findByRole("heading", { name: "Hello" })).toBeTruthy();
  });

  it("renders images through the authorized file route", async () => {
    render(
      <DocumentPreview sessionId="s1" path="a.png" services={services()} onClose={() => undefined} />,
    );
    const image = await screen.findByRole("img");
    expect(image.getAttribute("src")).toContain("/api/file?path=");
  });

  it("reports the missing office conversion", () => {
    render(
      <DocumentPreview sessionId="s1" path="report.docx" services={services()} onClose={() => undefined} />,
    );
    expect(screen.getByText(/officeToPdf/)).toBeTruthy();
  });
});