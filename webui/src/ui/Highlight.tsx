import type { ReactNode } from "react";

function escapeRe(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function Highlight({ text, terms }: { text: string; terms: string[] }) {
  if (!text) return null;
  if (terms.length === 0) return <>{text}</>;
  const re = new RegExp(`(${terms.map(escapeRe).join("|")})`, "gi");
  const parts = text.split(re);
  const nodes: ReactNode[] = [];
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    if (!part) continue;
    const isMatch = i % 2 === 1;
    nodes.push(isMatch ? <mark key={i}>{part}</mark> : <span key={i}>{part}</span>);
  }
  return <>{nodes}</>;
}