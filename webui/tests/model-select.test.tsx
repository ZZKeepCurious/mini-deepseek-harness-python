import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ModelSelect } from "../src/composer/ModelSelect";

describe("ModelSelect", () => {
  it("shows current provider/model and selects from catalog", () => {
    const onSelect = vi.fn(async () => null);
    render(
      <ModelSelect
        catalog={{
          default: { provider: "fake", model: "fake-model" },
          routableProviders: ["fake"],
          groups: [{ id: "fake", name: "fake", models: [{ id: "fake-model", name: "Fake Model" }] }],
          failures: [],
        }}
        selection={{ provider: "fake", model: "fake-model" }}
        locked={false}
        onSelect={onSelect}
      />,
    );
    expect(screen.getByRole("button", { name: /Select model, current fake\/fake-model/i })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Select model/i }));
    fireEvent.click(screen.getByRole("option", { name: /Fake Model/i }));
    expect(onSelect).toHaveBeenCalledWith("fake", "fake-model");
  });
});
