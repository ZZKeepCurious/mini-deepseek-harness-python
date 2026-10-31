import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReasoningRow } from "../src/ui/ReasoningRow";

describe("ReasoningRow", () => {
  it("renders compact markdown for reasoning text", () => {
    render(<ReasoningRow text="step **one**" />);
    expect(screen.getByText("one").tagName).toBe("STRONG");
  });
});
