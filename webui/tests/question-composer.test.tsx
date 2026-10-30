import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QuestionComposer } from "../src/ui/QuestionComposer";
import { clearQuestionDraft } from "../src/wire/question-draft";
import type { QuestionItem } from "../src/wire/questions";

afterEach(() => {
  clearQuestionDraft("q-1");
  clearQuestionDraft("q-2");
});

const QUESTIONS: QuestionItem[] = [
  {
    id: "profile",
    header: "偏好",
    question: "选择候选人类型",
    detail: "按当前空缺岗位的优先级选择。",
    options: [
      { label: "工程落地型 (Recommended)", description: "优先工程交付。" },
      { label: "研究潜力型", description: "优先研究能力。" },
    ],
  },
  { id: "detail", question: "补充你的要求" },
  {
    id: "signals",
    question: "选择重要信号（可多选）",
    multiSelect: true,
    options: [{ label: "系统设计" }, { label: "代码质量" }, { label: "产品判断" }],
  },
];

function renderComposer(overrides: {
  pendingKey?: string;
  questions?: QuestionItem[];
  onAnswer?: (answer: unknown) => Promise<void>;
  onDismiss?: () => Promise<void>;
} = {}) {
  return render(
    <QuestionComposer
      pendingKey={overrides.pendingKey ?? "q-1"}
      questions={overrides.questions ?? QUESTIONS}
      onAnswer={overrides.onAnswer ?? (async () => undefined)}
      onDismiss={overrides.onDismiss ?? (async () => undefined)}
    />,
  );
}

describe("QuestionComposer", () => {
  it("shows the first question, a recommended default, and cancel chrome", () => {
    renderComposer();
    expect(document.querySelector("[data-question-key]")?.getAttribute("data-question-key")).toBe("q-1");
    expect(screen.getByText("偏好")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "选择候选人类型" })).toBeTruthy();
    expect(screen.getByText("1 / 3")).toBeTruthy();
    expect(screen.getByText("推荐")).toBeTruthy();
    expect(screen.getByRole("radio", { name: "工程落地型" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByText("按当前空缺岗位的优先级选择。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "放弃整组问题" })).toBeTruthy();
    expect(screen.queryByText("会一直等你回答")).toBeNull();
    expect(screen.queryByRole("button", { name: "慢慢回答" })).toBeNull();
  });

  it("collects single, custom, and multi-select answers before one batch submit", async () => {
    const onAnswer = vi.fn(async () => undefined);
    renderComposer({ onAnswer });

    const detail = screen.getByText("按当前空缺岗位的优先级选择。");
    const scrollRegion = detail.closest("[data-question-scroll]");
    expect(scrollRegion?.contains(screen.getByRole("radio", { name: "工程落地型" }))).toBe(true);
    expect(scrollRegion?.contains(screen.getByText("下一题").closest("button"))).toBe(false);

    fireEvent.click(screen.getByRole("radio", { name: "工程落地型" }));
    expect(screen.getByText("2 / 3")).toBeTruthy();
    expect(screen.queryByText("按当前空缺岗位的优先级选择。")).toBeNull();

    const custom = screen.getByPlaceholderText("输入你的答案");
    fireEvent.change(custom, { target: { value: "要能独立排查线上问题" } });
    fireEvent.keyDown(custom, { key: "Enter" });

    expect(screen.getByText("3 / 3")).toBeTruthy();
    expect(screen.getByText("选择重要信号（可多选）")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "系统设计" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "系统设计" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "系统设计" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "代码质量" }));
    const multiCustom = screen.getByPlaceholderText("输入你的答案");
    fireEvent.change(multiCustom, { target: { value: "沟通能力" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "产品判断" }));
    fireEvent.keyDown(multiCustom, { key: "Enter" });

    expect(onAnswer).toHaveBeenCalledWith({
      answers: [
        { id: "profile", selected: ["工程落地型 (Recommended)"] },
        { id: "detail", selected: [], custom: "要能独立排查线上问题" },
        { id: "signals", selected: ["系统设计", "代码质量", "产品判断"], custom: "沟通能力" },
      ],
    });
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "正在提交…" }).disabled).toBe(true);
  });

  it("skips individual questions without discarding earlier answers", () => {
    const onAnswer = vi.fn(async () => undefined);
    renderComposer({ onAnswer });
    fireEvent.click(screen.getByRole("radio", { name: "研究潜力型" }));
    expect(screen.getByText("2 / 3")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "跳过" }));
    expect(screen.getByText("3 / 3")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "跳过" }));
    expect(onAnswer).toHaveBeenCalledWith({
      answers: [
        { id: "profile", selected: ["研究潜力型"] },
        { id: "detail", selected: [] },
        { id: "signals", selected: [] },
      ],
    });
  });

  it("treats Enter on an unselected option as submit, without selecting it", () => {
    const onAnswer = vi.fn(async () => undefined);
    renderComposer({
      onAnswer,
      questions: [{ id: "mode", question: "Which mode?", options: [{ label: "Alpha" }, { label: "Beta" }] }],
    });
    const option = screen.getByRole("radio", { name: "Alpha" });
    fireEvent.keyDown(option, { key: "Enter" });
    expect(option.getAttribute("aria-checked")).toBe("false");
    expect(screen.getByRole("status").textContent).toBe("请先完成这道问题。");
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("shows the inline custom input, reports missing answers, and supports pager navigation", () => {
    const onAnswer = vi.fn(async () => undefined);
    renderComposer({ onAnswer });
    expect(screen.getByPlaceholderText("输入你的答案")).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "工程落地型" }));
    const emptyCustom = screen.getByPlaceholderText("输入你的答案");
    fireEvent.keyDown(emptyCustom, { key: "Enter", shiftKey: true });
    expect(screen.getByText("2 / 3")).toBeTruthy();
    fireEvent.keyDown(emptyCustom, { key: "Enter" });
    expect(screen.getByText("请选择一个选项或填写自定义答案。")).toBeTruthy();

    fireEvent.click(screen.getByLabelText("下一题"));
    fireEvent.click(screen.getByRole("checkbox", { name: "产品判断" }));
    fireEvent.click(screen.getByRole("button", { name: "提交" }));
    expect(screen.getByText("请先完成这道问题。")).toBeTruthy();
    expect(screen.getByText("2 / 3")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("上一题"));
    expect(screen.getByText("1 / 3")).toBeTruthy();
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("keeps IME Enter inside the custom input until composition finishes", () => {
    const onAnswer = vi.fn(async () => undefined);
    renderComposer({ onAnswer });
    fireEvent.click(screen.getByRole("radio", { name: "研究潜力型" }));
    const custom = screen.getByPlaceholderText("输入你的答案");
    fireEvent.change(custom, { target: { value: "中文输入" } });
    fireEvent.keyDown(custom, { key: "Enter", isComposing: true });
    expect(screen.getByText("2 / 3")).toBeTruthy();
    fireEvent.keyDown(custom, { key: "Enter", keyCode: 229 });
    expect(screen.getByText("2 / 3")).toBeTruthy();
    expect(onAnswer).not.toHaveBeenCalled();
    fireEvent.keyDown(custom, { key: "Enter" });
    expect(screen.getByText("3 / 3")).toBeTruthy();
  });

  it("grows both answer fields with the draft and keeps Shift+Enter a newline", () => {
    const onAnswer = vi.fn(async () => undefined);
    renderComposer({ onAnswer });
    const inline = screen.getByPlaceholderText("输入你的答案");
    expect(inline.tagName).toBe("TEXTAREA");
    const multiline = "第一行\n第二行";
    fireEvent.change(inline, { target: { value: multiline } });
    expect(inline.previousElementSibling?.textContent).toBe(`${multiline}\n`);
    fireEvent.keyDown(inline, { key: "Enter", shiftKey: true });
    expect(screen.getByText("1 / 3")).toBeTruthy();

    fireEvent.keyDown(inline, { key: "Enter" });
    const optionless = screen.getByPlaceholderText("输入你的答案");
    expect(optionless.tagName).toBe("TEXTAREA");
    fireEvent.change(optionless, { target: { value: multiline } });
    expect(optionless.previousElementSibling?.textContent).toBe(`${multiline}\n`);
    fireEvent.keyDown(optionless, { key: "Enter", shiftKey: true });
    expect(screen.getByText("2 / 3")).toBeTruthy();

    fireEvent.keyDown(optionless, { key: "Enter" });
    fireEvent.click(screen.getByRole("checkbox", { name: "系统设计" }));
    fireEvent.click(screen.getByRole("button", { name: "提交" }));
    expect(onAnswer).toHaveBeenCalledWith({
      answers: [
        { id: "profile", selected: [], custom: multiline },
        { id: "detail", selected: [], custom: multiline },
        { id: "signals", selected: ["系统设计"] },
      ],
    });
  });

  it("renders detail through MarkdownText", () => {
    const view = renderComposer({
      questions: [{
        id: "plan",
        question: "批准这个计划吗？",
        detail: "# 实施计划\n\n- **先验证**现状\n- 修改 `QuestionComposer`",
        options: [{ label: "批准" }],
      }],
    });
    expect(screen.getByRole("heading", { level: 1, name: "实施计划" })).toBeTruthy();
    expect(view.container.querySelector("strong")?.textContent).toBe("先验证");
    expect(view.container.querySelector("code")?.textContent).toBe("QuestionComposer");
    expect(view.container.querySelectorAll("li")).toHaveLength(2);
  });

  it("collapses to the header strip and expands without losing drafts", () => {
    const onAnswer = vi.fn(async () => undefined);
    renderComposer({ onAnswer });
    expect(screen.getByRole("radiogroup")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("收起问题卡片"));
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(screen.getByText("选择候选人类型")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("展开问题卡片"));
    fireEvent.click(screen.getByRole("radio", { name: "工程落地型" }));
    expect(screen.getByText("2 / 3")).toBeTruthy();
    const custom = screen.getByPlaceholderText("输入你的答案");
    fireEvent.change(custom, { target: { value: "要能独立排查线上问题" } });
    fireEvent.click(screen.getByLabelText("下一题"));
    fireEvent.click(screen.getByRole("checkbox", { name: "系统设计" }));
    fireEvent.click(screen.getByRole("button", { name: "提交" }));
    expect(onAnswer).toHaveBeenCalledWith({
      answers: [
        { id: "profile", selected: ["工程落地型 (Recommended)"] },
        { id: "detail", selected: [], custom: "要能独立排查线上问题" },
        { id: "signals", selected: ["系统设计"] },
      ],
    });
  });

  it("restores the current page and drafts after remount", () => {
    const view = renderComposer();
    fireEvent.click(screen.getByRole("radio", { name: "研究潜力型" }));
    const custom = screen.getByPlaceholderText("输入你的答案");
    fireEvent.change(custom, { target: { value: "保留这段草稿" } });
    expect(screen.getByText("2 / 3")).toBeTruthy();
    view.unmount();
    renderComposer();
    expect(screen.getByText("2 / 3")).toBeTruthy();
    expect(screen.getByPlaceholderText<HTMLTextAreaElement>("输入你的答案").value).toBe("保留这段草稿");
    fireEvent.click(screen.getByLabelText("上一题"));
    expect(screen.getByRole("radio", { name: "研究潜力型" }).getAttribute("aria-checked")).toBe("true");
  });

  it("resets drafts when a different request takes the seat", () => {
    const view = renderComposer();
    fireEvent.click(screen.getByRole("radio", { name: "研究潜力型" }));
    expect(screen.getByText("2 / 3")).toBeTruthy();
    view.rerender(
      <QuestionComposer
        pendingKey="q-2"
        questions={QUESTIONS}
        onAnswer={async () => undefined}
        onDismiss={async () => undefined}
      />,
    );
    expect(screen.getByRole("radio", { name: "工程落地型" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByText("1 / 3")).toBeTruthy();
  });

  it("surfaces cancellation failures and re-arms the controls", async () => {
    const onDismiss = vi.fn()
      .mockRejectedValueOnce(new Error("第一次取消失败"))
      .mockRejectedValueOnce(new Error("第二次取消失败"));
    renderComposer({ onDismiss });
    fireEvent.click(screen.getByRole("button", { name: "放弃整组问题" }));
    expect(await screen.findByText("第一次取消失败")).toBeTruthy();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "跳过" }).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "放弃整组问题" }));
    expect(await screen.findByText("第二次取消失败")).toBeTruthy();
  });

  it("surfaces answer rejection and re-arms submit", async () => {
    const onAnswer = vi.fn()
      .mockRejectedValueOnce(new Error("网络中断"))
      .mockRejectedValueOnce("字符串错误");
    renderComposer({
      onAnswer,
      questions: [{ id: "detail", question: "补充你的要求" }],
    });
    fireEvent.change(screen.getByPlaceholderText("输入你的答案"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "提交" }));
    expect(await screen.findByText("网络中断")).toBeTruthy();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "提交" }).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "提交" }));
    expect(await screen.findByText("字符串错误")).toBeTruthy();
  });

  it("shows a timed countdown and take-time, then the held label", () => {
    const onTakeTime = vi.fn();
    render(
      <QuestionComposer
        pendingKey="q-timed"
        questions={[{ id: "scope", question: "选择范围", options: [{ label: "仅工具" }] }]}
        onAnswer={async () => undefined}
        onDismiss={async () => undefined}
        clock={{
          waitState: "counting",
          remainingMs: 1500,
          running: true,
          channel: "waterfall",
          dismissal: "hide",
        }}
        onTakeTime={onTakeTime}
      />,
    );
    expect(screen.getByText("2 秒后继续工作")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "慢慢回答" }));
    expect(onTakeTime).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("收起问题面板，可从工具调用重新打开")).toBeTruthy();
  });

  it("disables submit after timeout and re-arms once continued", () => {
    const view = render(
      <QuestionComposer
        pendingKey="q-timed"
        questions={[{ id: "scope", question: "选择范围", options: [{ label: "仅工具" }] }]}
        onAnswer={async () => undefined}
        onDismiss={async () => undefined}
        clock={{
          waitState: "counting",
          channel: "none",
          dismissal: "hide",
        }}
      />,
    );
    expect(screen.queryByText(/秒后继续工作/)).toBeNull();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "提交" }).disabled).toBe(true);
    view.rerender(
      <QuestionComposer
        pendingKey="q-timed"
        questions={[{ id: "scope", question: "选择范围", options: [{ label: "仅工具" }] }]}
        onAnswer={async () => undefined}
        onDismiss={async () => undefined}
        clock={{
          waitState: "continued",
          channel: "rpc",
          dismissal: "hide",
        }}
      />,
    );
    expect(screen.getByText("已继续工作，仍可回答")).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "仅工具" }));
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "提交" }).disabled).toBe(false);
  });
});
