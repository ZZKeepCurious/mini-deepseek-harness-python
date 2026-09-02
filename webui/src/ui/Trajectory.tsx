// Trajectory — virtualized trajectory view with Overview bar, per-turn
// collapse and full-text search (R5).
//
// Data flows from the wire trajectory buffer (session.follow) as
// EventEnvelope[]. Rendering here is windowed: the event log is projected into
// measurable virtual rows (src/trajectory/model.ts) and only the rows inside
// the scroll viewport (± overscan) are mounted, so sessions with thousands of
// events stay bounded in DOM size. Overview, search and collapse semantics
// mirror the upstream ui-trajectory concepts (virtual-rows / collapsed-summary
// / trajectory-search-index).

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { EventEnvelope } from "../wire";
import {
  OVERSCAN_ROWS,
  applyCollapse,
  buildOverview,
  groupRows,
  offsetOfRow,
  windowedRows,
} from "../trajectory/model";
import type { TrajectoryRow } from "../trajectory/model";
import { TrajectorySearchIndex } from "../trajectory/search";

interface Props {
  events: EventEnvelope[];
  running: boolean;
}

export function Trajectory({ events, running }: Props) {
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const [viewport, setViewport] = useState({ top: 0, height: 480 });
  const [collapsed, setCollapsed] = useState<ReadonlySet<number>>(() => new Set());
  const [query, setQuery] = useState("");

  const baseRows = useMemo(() => groupRows(events), [events]);
  const overview = useMemo(() => buildOverview(baseRows), [baseRows]);
  const index = useMemo(() => {
    const ix = new TrajectorySearchIndex();
    ix.update(baseRows);
    return ix;
  }, [baseRows]);
  const hits = useMemo(() => (query.trim() ? index.search(query) : null), [index, query]);

  const rows = useMemo<TrajectoryRow[]>(() => {
    if (hits) return baseRows.filter((r) => hits.has(r.key));
    return applyCollapse(baseRows, collapsed);
  }, [baseRows, collapsed, hits]);

  const win = useMemo(
    () => windowedRows(rows, viewport.top, viewport.height, OVERSCAN_ROWS),
    [rows, viewport],
  );
  const visible = useMemo(() => rows.slice(win.start, win.end), [rows, win]);

  const setViewportFromEl = () => {
    const el = scrollerRef.current;
    if (!el) return;
    setViewport({ top: el.scrollTop, height: el.clientHeight || 480 });
  };

  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (el) setViewport({ top: el.scrollTop, height: el.clientHeight || 480 });
  }, [rows.length, events]);

  const jumpToTurn = (turn: number) => {
    setQuery("");
    setCollapsed((prev) => {
      if (!prev.has(turn)) return prev;
      const next = new Set(prev);
      next.delete(turn);
      return next;
    });
    const el = scrollerRef.current;
    if (!el) return;
    const idx = baseRows.findIndex((r) => r.kind === "turn" && r.turn === turn);
    if (idx < 0) return;
    el.scrollTop = Math.max(0, offsetOfRow(baseRows, idx) - 8);
    setViewport({ top: el.scrollTop, height: el.clientHeight || 480 });
  };

  const toggleCollapseAll = () => {
    setCollapsed((prev) =>
      prev.size === 0 ? new Set(overview.map((t) => t.turn)) : new Set(),
    );
  };

  const expandTurn = (turn: number) => {
    setCollapsed((prev) => {
      if (!prev.has(turn)) return prev;
      const next = new Set(prev);
      next.delete(turn);
      return next;
    });
  };

  const terms = useMemo(() => buildTerms(query), [query]);
  const toolbar = (
    <div className="trajectory-toolbar">
      <input
        className="input searchbox"
        placeholder="搜索会话轨迹…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {hits && (
        <span className={`matchcount${hits.size === 0 ? " none" : ""}`}>
          {hits.size === 0 ? "无匹配" : `命中 ${hits.size} 行`}
        </span>
      )}
      <button className="btn small ghost" onClick={toggleCollapseAll}>
        {collapsed.size === 0 ? "折叠全部" : "展开全部"}
      </button>
    </div>
  );

  if (events.length === 0) {
    return (
      <div className="trajectory">
        {toolbar}
        <div className="trajectory-scroll">
          <div className="empty">跟随会话中…（等待事件）</div>
        </div>
      </div>
    );
  }

  return (
    <div className="trajectory">
      {toolbar}
      <div className="overview-bar">
        {overview.map((t) => (
          <button
            key={t.turn}
            className={`turn-chip${collapsed.has(t.turn) ? " collapsed" : ""}`}
            title={t.preview || `回合 ${t.turn}`}
            onClick={() => jumpToTurn(t.turn)}
          >
            回合 {t.turn} · {t.messageCount} 条消息
            {t.toolCount > 0 ? ` · ${t.toolCount} 工具` : ""}
          </button>
        ))}
      </div>
      <div className="trajectory-scroll" ref={scrollerRef} onScroll={setViewportFromEl}>
        {win.topPad > 0 && <div style={{ height: win.topPad }} aria-hidden="true" />}
        {visible.map((row) => (
          <RowView
            key={row.key}
            row={row}
            terms={terms}
            matched={hits !== null && hits.has(row.key)}
            onExpand={expandTurn}
          />
        ))}
        {win.bottomPad > 0 && <div style={{ height: win.bottomPad }} aria-hidden="true" />}
        {rows.length === 0 && <div className="empty">无匹配</div>}
        {running && <div className="empty dim">agent 运行中…</div>}
      </div>
    </div>
  );
}

function RowView({
  row,
  terms,
  matched,
  onExpand,
}: {
  row: TrajectoryRow;
  terms: string[];
  matched: boolean;
  onExpand: (turn: number) => void;
}) {
  if (row.kind === "turn") {
    return <div className="vrow turn-head">— turn {row.turn} —</div>;
  }
  if (row.kind === "summary") {
    return (
      <button type="button" className="vrow summary" onClick={() => onExpand(row.turn)}>
        <span className="who">回合 {row.turn}</span>
        <span className="sum-text">{row.preview || "（无文本）"}</span>
        <span className="dim sum-meta">
          · {row.count} 条消息{row.toolCount > 0 ? ` · ${row.toolCount} 工具` : ""}
        </span>
      </button>
    );
  }
  if (row.kind === "message") {
    return (
      <div className={`vrow msg ${row.role === "user" ? "user" : "assistant"}${matched ? " hit" : ""}`}>
        <span className="who">{row.role}</span>
        {row.blocks.map((blk, i) => (
          <div key={i} className={blk.type === "reasoning" ? "block reasoning" : "block"}>
            <Highlight text={blk.text} terms={terms} />
          </div>
        ))}
      </div>
    );
  }
  return (
    <div className={`vrow tool${row.isError ? " error" : ""}${matched ? " hit" : ""}`}>
      <div className="tool-call2">
        <span className="label">{row.isError ? "工具错误:" : "工具:"}</span> {row.name}(
        <Highlight text={row.arguments} terms={terms} />)
      </div>
      {row.result && (
        <div className="tool-result2">
          <Highlight text={row.result} terms={terms} />
        </div>
      )}
    </div>
  );
}

function buildTerms(query: string): string[] {
  return query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function Highlight({ text, terms }: { text: string; terms: string[] }) {
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