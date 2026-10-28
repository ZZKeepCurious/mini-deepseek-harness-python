export type RightView = "console" | "files" | "terminal";

export interface DockLayout {
  view: RightView;
  width: number;
  collapsed: boolean;
  fullscreen: boolean;
}

export const DOCK_MIN_WIDTH = 260;
export const DOCK_MAX_WIDTH = 720;
export const DOCK_DEFAULT_WIDTH = 320;

const LAYOUT_KEY = "dsh.rightdock.v1";

function viewportWidth(): number {
  return typeof window === "undefined" ? 1280 : window.innerWidth;
}

export function maxDockWidth(viewport = viewportWidth()): number {
  return Math.max(DOCK_MIN_WIDTH, Math.min(DOCK_MAX_WIDTH, Math.round(viewport * 0.6)));
}

export function clampDockWidth(width: number, viewport = viewportWidth()): number {
  const finite = Number.isFinite(width) ? width : DOCK_DEFAULT_WIDTH;
  return Math.max(DOCK_MIN_WIDTH, Math.min(maxDockWidth(viewport), Math.round(finite)));
}

export function narrowViewport(viewport = viewportWidth()): boolean {
  return viewport < 768;
}

export function defaultDockLayout(collapsed = true): DockLayout {
  return {
    view: "console",
    width: clampDockWidth(DOCK_DEFAULT_WIDTH),
    collapsed,
    fullscreen: false,
  };
}

function storageKey(sessionId: string): string {
  return `${LAYOUT_KEY}.${sessionId}`;
}

function isView(value: unknown): value is RightView {
  return value === "console" || value === "files" || value === "terminal";
}

/**
 * Restore one session's dock layout. Unknown or malformed fields fall back to
 * the default as a group, so a partially written record cannot produce a dock
 * with, for example, no usable width. A narrow viewport promotes an expanded
 * normal panel to fullscreen, matching the upstream opening rule.
 */
export function loadDockLayout(sessionId: string | null): DockLayout {
  const fallback = defaultDockLayout();
  if (sessionId === null || typeof localStorage === "undefined") return fallback;
  try {
    const raw = localStorage.getItem(storageKey(sessionId));
    if (raw === null) return fallback;
    const parsed = JSON.parse(raw) as Partial<DockLayout> | null;
    if (
      parsed === null ||
      !isView(parsed.view) ||
      typeof parsed.width !== "number" ||
      !Number.isFinite(parsed.width) ||
      typeof parsed.collapsed !== "boolean" ||
      typeof parsed.fullscreen !== "boolean"
    ) {
      return fallback;
    }
    const collapsed = parsed.collapsed;
    return {
      view: parsed.view,
      width: clampDockWidth(parsed.width),
      collapsed,
      fullscreen: collapsed ? false : parsed.fullscreen || narrowViewport(),
    };
  } catch {
    return fallback;
  }
}

export function saveDockLayout(sessionId: string | null, layout: DockLayout): void {
  if (sessionId === null || typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(storageKey(sessionId), JSON.stringify(layout));
  } catch {
    return;
  }
}
