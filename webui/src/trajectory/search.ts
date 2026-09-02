// Incremental full-text index over trajectory rows.
//
// Mirrors the upstream ui-trajectory search index (packages/client/
// ui-trajectory/src/client/trajectory-search-index.ts): rows publish sources
// (turn number, role/kind, tool name/arguments/result, message text …);
// `search` matches space-separated case-insensitive AND terms and returns the
// row keys that hit. `update` short-circuits on the same array reference and
// prunes stale entries, so repeated renders stay cheap.
//
// Pure data structure — no UI.

import type { TrajectoryRow } from "./model";
import { previewLine } from "./model";

interface Entry {
  sources: readonly string[];
  text: string;
}

export function rowSources(row: TrajectoryRow): string[] {
  if (row.kind === "message") {
    return [`turn ${row.turn}`, "message", row.role, row.text, previewLine(row.text, 120)];
  }
  if (row.kind === "tool") {
    return [
      `turn ${row.turn}`,
      "tool",
      row.name,
      row.arguments,
      row.result,
      ...(row.isError ? ["tool error"] : []),
    ];
  }
  if (row.kind === "summary") {
    return [`turn ${row.turn}`, "summary", row.preview];
  }
  return [`turn ${row.turn}`, "turn"];
}

function sameSources(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

export class TrajectorySearchIndex {
  private entries = new Map<string, Entry>();
  private current: readonly TrajectoryRow[] | undefined;

  // Reindex `rows`. Returns true when the indexed documents changed.
  update(rows: readonly TrajectoryRow[]): boolean {
    if (this.current === rows) return false;
    this.current = rows;
    const seen = new Set<string>();
    let changed = false;
    for (const row of rows) {
      seen.add(row.key);
      const sources = rowSources(row);
      const previous = this.entries.get(row.key);
      if (previous && sameSources(previous.sources, sources)) continue;
      this.entries.set(row.key, { sources, text: sources.join("\n").toLocaleLowerCase() });
      changed = true;
    }
    for (const key of Array.from(this.entries.keys())) {
      if (!seen.has(key)) {
        this.entries.delete(key);
        changed = true;
      }
    }
    return changed;
  }

  // Search terms (space separated, case insensitive, AND semantics). Returns
  // null for an empty query; otherwise the set of matching row keys.
  search(query: string): ReadonlySet<string> | null {
    const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    if (terms.length === 0) return null;
    const hits = new Set<string>();
    for (const [key, entry] of this.entries) {
      if (terms.every((t) => entry.text.includes(t))) hits.add(key);
    }
    return hits;
  }
}