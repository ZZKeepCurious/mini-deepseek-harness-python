import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MarkdownText } from "../src/ui/MarkdownText";
import { Trajectory } from "../src/ui/Trajectory";
import { assistantMsg } from "./fixtures";

describe("MarkdownText", () => {
  it("renders GFM headings, strong text and tables", () => {
    const { container } = render(
      <MarkdownText text={"# Title\n\n**bold**\n\n| a | b |\n|---|---|\n| 1 | 2 |"} />,
    );
    expect(container.querySelector("h1")?.textContent).toBe("Title");
    expect(container.querySelector("strong")?.textContent).toBe("bold");
    expect(container.querySelector("table th")?.textContent).toBe("a");
    expect(container.querySelector("table td")?.textContent).toBe("1");
  });

  it("renders TeX through KaTeX", () => {
    const { container } = render(<MarkdownText text={"$x^2$"} />);
    expect(container.querySelector(".katex")).toBeTruthy();
    expect(container.textContent).toContain("x");
  });

  it("resolves absolute local images through the authenticated file route", () => {
    const { container } = render(<MarkdownText text={"![plot](C:/tmp/plot.png)"} />);
    const image = container.querySelector("img");
    expect(image?.getAttribute("src")).toBe("/api/file?path=C%3A%2Ftmp%2Fplot.png");
    expect(image?.getAttribute("loading")).toBe("lazy");
  });

  it("keeps remote images direct and refuses unsafe links", () => {
    const { container } = render(
      <MarkdownText text={"![x](https://example.com/x.png)\n\n[bad](javascript:alert(1))"} />,
    );
    expect(container.querySelector("img")?.getAttribute("src")).toBe("https://example.com/x.png");
    expect(container.innerHTML).not.toContain("javascript:");
  });

  it("does not execute raw HTML", () => {
    const { container } = render(<MarkdownText text={"<script>alert(1)</script>"} />);
    expect(container.querySelector("script")).toBeNull();
  });

  it("marks search terms inside rendered markdown", () => {
    const { container } = render(
      <MarkdownText text={"hello **world**"} highlightTerms={["world"]} />,
    );
    expect(container.querySelectorAll("mark")).toHaveLength(1);
    expect(container.querySelector("mark")?.textContent).toBe("world");
  });

  it("renders assistant trajectory text as markdown", () => {
    render(<Trajectory events={[assistantMsg(1, "**bold answer**")]} running={false} />);
    expect(screen.getByText("bold answer").tagName).toBe("STRONG");
  });
});
