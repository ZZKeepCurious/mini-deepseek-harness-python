// Terminal turn notices (src/ui/TurnNotice.tsx): token-cap copy, stopped turns,
// localized provider codes and the verbatim fallback.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { TurnNotice } from "../src/ui/TurnNotice";

describe("TurnNotice", () => {
  it("explains the output-token cap without a code badge", () => {
    render(<TurnNotice notice="max-tokens" failure={null} />);
    expect(screen.getByText("已达到输出 token 上限")).toBeTruthy();
    expect(screen.getByText(/回答被截断/)).toBeTruthy();
    expect(document.querySelector(".turn-notice-code")).toBeNull();
  });

  it("localizes provider codes and keeps the code visible", () => {
    render(<TurnNotice notice="error" failure={{ code: "QUOTA", message: "no balance" }} />);
    expect(screen.getByText("本轮运行失败")).toBeTruthy();
    expect(screen.getByText("当前请求的额度已用尽")).toBeTruthy();
    expect(screen.getByText("QUOTA")).toBeTruthy();
  });

  it("shows a stopped account task with its product copy", () => {
    render(
      <TurnNotice
        notice="stopped"
        failure={{ code: "ACCOUNT_SIGNED_OUT", message: "Stopped because you signed out." }}
      />,
    );
    expect(screen.getByText("任务已停止")).toBeTruthy();
    expect(screen.getByText("任务已因退出 DeepSeek 登录而停止。")).toBeTruthy();
  });

  it("hides the badge for restart recovery and shows any other message verbatim", () => {
    render(
      <TurnNotice
        notice="stopped"
        failure={{ code: "INTERRUPTED", message: "The DSH process exited." }}
      />,
    );
    expect(screen.getByText(/DSH 进程在本轮完成前退出/)).toBeTruthy();
    expect(document.querySelector(".turn-notice-code")).toBeNull();

    render(
      <TurnNotice notice="error" failure={{ code: "PROVIDER", message: "upstream exploded" }} />,
    );
    expect(screen.getByText("upstream exploded")).toBeTruthy();
    expect(screen.getByText("PROVIDER")).toBeTruthy();
  });

  it("renders nothing for an error notice without a recorded failure", () => {
    const { container } = render(<TurnNotice notice="error" failure={null} />);
    expect(container.firstChild).toBeNull();
  });
});
