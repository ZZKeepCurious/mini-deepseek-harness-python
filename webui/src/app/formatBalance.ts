// Platform Web currency formatting for account balances (upstream
// ui-settings-account/src/client/formatBalance.ts): source amounts keep decimal
// precision, so the arithmetic runs on Big rather than IEEE doubles.

import { Big } from "big.js";

/**
 * Format a validated decimal balance with Platform Web's two-decimal and
 * sub-cent display rules.
 * @param amount - validated decimal balance string.
 * @param symbol - currency symbol.
 * @returns signed currency text with grouped digits.
 */
export function formatBalance(amount: string, symbol: "¥" | "$"): string {
  const value = new Big(amount);
  if (value.eq(0)) return `${symbol}0.00`;
  if (value.lt(0)) return `-${symbol}${value.gt(-0.01) ? "0.01" : addCommas(value.abs().toFixed(2))}`;
  if (value.lt("0.01")) return `<${symbol}0.01`;
  return `${symbol}${addCommas(value.round(2, Big.roundDown).toFixed(2))}`;
}

function addCommas(value: string): string {
  const [integer, fraction] = value.split(".");
  return `${Number(integer).toLocaleString()}.${fraction}`;
}

/** The symbol one balance currency renders with. */
export function balanceSymbol(currency: string): "¥" | "$" {
  return currency === "CNY" ? "¥" : "$";
}
