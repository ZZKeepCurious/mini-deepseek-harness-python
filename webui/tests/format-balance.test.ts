// Platform Web balance formatting (src/app/formatBalance.ts): two decimals,
// sub-cent rounding, grouped digits and signed amounts.

import { describe, expect, it } from "vitest";
import { balanceSymbol, formatBalance } from "../src/app/formatBalance";

describe("formatBalance", () => {
  it("keeps two decimals and groups integer digits", () => {
    expect(formatBalance("1234.5", "¥")).toBe("¥1,234.50");
    expect(formatBalance("0", "$")).toBe("$0.00");
    expect(formatBalance("1000000", "¥")).toBe("¥1,000,000.00");
    // The Platform wire schema can carry exponent notation, which Big parses natively.
    expect(formatBalance("5e0", "¥")).toBe("¥5.00");
    expect(formatBalance("0E-16", "¥")).toBe("¥0.00");
  });

  it("shows positive sub-cent balances as a lower bound", () => {
    expect(formatBalance("0.004", "¥")).toBe("<¥0.01");
    expect(formatBalance("0.000001", "$")).toBe("<$0.01");
    expect(formatBalance("1E-8", "¥")).toBe("<¥0.01");
  });

  it("rounds down the displayed cents instead of rounding up the balance", () => {
    expect(formatBalance("1.239", "¥")).toBe("¥1.23");
    expect(formatBalance("1.999", "¥")).toBe("¥1.99");
  });

  it("keeps the negative sign and clamps values that round to zero cents", () => {
    // Negative amounts use Big's default rounding, so a negative debt is never understated.
    expect(formatBalance("-1.239", "¥")).toBe("-¥1.24");
    expect(formatBalance("-12.345", "¥")).toBe("-¥12.35");
    expect(formatBalance("-0.004", "$")).toBe("-$0.01");
  });
});

describe("balanceSymbol", () => {
  it("maps the wallet currency to the Platform symbol", () => {
    expect(balanceSymbol("CNY")).toBe("¥");
    expect(balanceSymbol("USD")).toBe("$");
  });
});
