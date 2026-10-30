import { useRef } from "react";
import type { KeyboardEvent, PointerEvent, ReactNode } from "react";
import {
  DOCK_MIN_WIDTH,
  maxDockWidth,
  type DockLayout,
  type RightView,
} from "../app/rightDock";

interface Props {
  layout: DockLayout;
  top?: ReactNode;
  onView: (view: RightView) => void;
  onWidth: (width: number) => void;
  onCollapse: () => void;
  onToggleFullscreen: () => void;
  children: ReactNode;
}

const VIEWS: Array<{ id: RightView; label: string }> = [
  { id: "console", label: "控制台" },
  { id: "schedule", label: "日程" },
  { id: "files", label: "文件" },
  { id: "terminal", label: "终端" },
];

export function RightDock({
  layout,
  top,
  onView,
  onWidth,
  onCollapse,
  onToggleFullscreen,
  children,
}: Props) {
  const drag = useRef<{ x: number; width: number } | null>(null);

  const onPointerDown = (event: PointerEvent<HTMLDivElement>): void => {
    if (layout.fullscreen) return;
    drag.current = { x: event.clientX, width: layout.width };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  };

  const onPointerMove = (event: PointerEvent<HTMLDivElement>): void => {
    const start = drag.current;
    if (start === null) return;
    onWidth(start.width - (event.clientX - start.x));
  };

  const stopDrag = (event: PointerEvent<HTMLDivElement>): void => {
    if (drag.current === null) return;
    drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const onResizeKey = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (layout.fullscreen) return;
    if (event.key === "ArrowLeft") onWidth(layout.width + 16);
    else if (event.key === "ArrowRight") onWidth(layout.width - 16);
    else if (event.key === "Home") onWidth(DOCK_MIN_WIDTH);
    else if (event.key === "End") onWidth(maxDockWidth());
    else return;
    event.preventDefault();
  };

  return (
    <aside
      className={`panel right-panel${layout.fullscreen ? " fullscreen" : ""}`}
      aria-label="右侧栏"
    >
      <div
        className="right-resize"
        role="separator"
        aria-label="调整右侧栏宽度"
        aria-orientation="vertical"
        aria-valuemin={DOCK_MIN_WIDTH}
        aria-valuemax={maxDockWidth()}
        aria-valuenow={layout.width}
        tabIndex={layout.fullscreen ? -1 : 0}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={stopDrag}
        onPointerCancel={stopDrag}
        onKeyDown={onResizeKey}
      />
      {top}
      <div className="panel-header right-tabs" role="tablist" aria-label="右侧栏">
        {VIEWS.map((view) => (
          <button
            key={view.id}
            type="button"
            role="tab"
            aria-selected={layout.view === view.id}
            className={`view-tab${layout.view === view.id ? " active" : ""}`}
            onClick={() => onView(view.id)}
          >
            {view.label}
          </button>
        ))}
        <span className="spacer" />
        <div className="dock-controls">
          <button
            type="button"
            className="dock-control"
            aria-label={layout.fullscreen ? "还原右侧栏" : "全屏右侧栏"}
            aria-pressed={layout.fullscreen}
            title={layout.fullscreen ? "还原右侧栏" : "全屏右侧栏"}
            onClick={onToggleFullscreen}
          >
            {layout.fullscreen ? "还原" : "全屏"}
          </button>
          <button
            type="button"
            className="dock-control"
            aria-label="收起右侧栏"
            title="收起右侧栏"
            onClick={onCollapse}
          >
            ×
          </button>
        </div>
      </div>
      <div className="right-body">{children}</div>
    </aside>
  );
}
