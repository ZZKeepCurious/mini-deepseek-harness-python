// Browser-side display preferences for the webui.
//
// Upstream keeps these in the host user-settings document (namespaces
// `ui-theme`, `ui-chat`, `ui-conversation`); the miniharness web backend mounts
// only plugin configuration namespaces, so the client owns them in one
// versioned browser record and applies the typography/theme to the document.
// The record is the single source for the Settings dialog and for every
// consumer (composer busy-Enter, coding view, terminal font).

export const THEME_PREFERENCES = ["light", "dark", "system"] as const;
export type ThemePreference = (typeof THEME_PREFERENCES)[number];

export const TRANSCRIPT_VIEW_MODES = ["compact", "standard", "detailed", "verbose"] as const;
export type TranscriptViewMode = (typeof TRANSCRIPT_VIEW_MODES)[number];

export const COLLAPSE_TIMINGS = ["completion", "next-input"] as const;
export type CollapseTiming = (typeof COLLAPSE_TIMINGS)[number];

export const PERFORMANCE_USAGE_MODES = ["compact", "detailed"] as const;
export type PerformanceUsageMode = (typeof PERFORMANCE_USAGE_MODES)[number];

export const BUSY_ENTER_BEHAVIORS = ["queue", "steer"] as const;
export type BusyEnterBehavior = (typeof BUSY_ENTER_BEHAVIORS)[number];

export const FONT_SIZE_MIN = 10;
export const FONT_SIZE_MAX = 22;
export const CODE_FONT_SIZE_MIN = 10;
export const CODE_FONT_SIZE_MAX = 16;
export const TERMINAL_FONT_SIZE_MIN = 10;
export const TERMINAL_FONT_SIZE_MAX = 20;

export interface UiPreferences {
  theme: ThemePreference;
  /** Conversation content size in px. */
  fontSize: number;
  /** Code-block size in px. */
  codeFontSize: number;
  /** Sidebar terminal cell size in px. */
  terminalFontSize: number;
  /** Font lists placed before the built-in stack; `''` keeps the stack. */
  textFontFamily: string;
  codeFontFamily: string;
  terminalFontFamily: string;
  transcriptView: TranscriptViewMode;
  collapseTiming: CollapseTiming;
  performanceUsage: PerformanceUsageMode;
  showCodingView: boolean;
  busyEnter: BusyEnterBehavior;
  uploadSessionLog: boolean;
}

export const DEFAULT_PREFERENCES: UiPreferences = Object.freeze({
  theme: "system",
  fontSize: 14,
  codeFontSize: 11,
  terminalFontSize: 13,
  textFontFamily: "",
  codeFontFamily: "",
  terminalFontFamily: "",
  transcriptView: "detailed",
  collapseTiming: "completion",
  performanceUsage: "detailed",
  showCodingView: true,
  busyEnter: "queue",
  uploadSessionLog: false,
});

const STORAGE_KEY = "dsh.uiPrefs.v1";
const LEGACY_BUSY_ENTER_KEY = "miniharness.busyEnter";

type Listener = (preferences: UiPreferences) => void;

let current: UiPreferences | null = null;
const listeners = new Set<Listener>();

function storage(): Storage | null {
  if (typeof localStorage === "undefined") return null;
  return localStorage;
}

function clampInteger(value: unknown, min: number, max: number, fallback: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  return Math.max(min, Math.min(max, Math.round(value)));
}

function oneOf<T extends string>(value: unknown, options: readonly T[], fallback: T): T {
  return typeof value === "string" && (options as readonly string[]).includes(value)
    ? (value as T)
    : fallback;
}

function stringOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** Normalize one stored record; malformed fields fall back one by one. */
export function normalizePreferences(raw: unknown): UiPreferences {
  const record = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
  return {
    theme: oneOf(record.theme, THEME_PREFERENCES, DEFAULT_PREFERENCES.theme),
    fontSize: clampInteger(record.fontSize, FONT_SIZE_MIN, FONT_SIZE_MAX, DEFAULT_PREFERENCES.fontSize),
    codeFontSize: clampInteger(
      record.codeFontSize, CODE_FONT_SIZE_MIN, CODE_FONT_SIZE_MAX, DEFAULT_PREFERENCES.codeFontSize,
    ),
    terminalFontSize: clampInteger(
      record.terminalFontSize,
      TERMINAL_FONT_SIZE_MIN,
      TERMINAL_FONT_SIZE_MAX,
      DEFAULT_PREFERENCES.terminalFontSize,
    ),
    textFontFamily: stringOf(record.textFontFamily),
    codeFontFamily: stringOf(record.codeFontFamily),
    terminalFontFamily: stringOf(record.terminalFontFamily),
    transcriptView: oneOf(
      record.transcriptView, TRANSCRIPT_VIEW_MODES, DEFAULT_PREFERENCES.transcriptView,
    ),
    collapseTiming: oneOf(
      record.collapseTiming, COLLAPSE_TIMINGS, DEFAULT_PREFERENCES.collapseTiming,
    ),
    performanceUsage: oneOf(
      record.performanceUsage, PERFORMANCE_USAGE_MODES, DEFAULT_PREFERENCES.performanceUsage,
    ),
    showCodingView: typeof record.showCodingView === "boolean"
      ? record.showCodingView
      : DEFAULT_PREFERENCES.showCodingView,
    busyEnter: oneOf(record.busyEnter, BUSY_ENTER_BEHAVIORS, DEFAULT_PREFERENCES.busyEnter),
    uploadSessionLog: typeof record.uploadSessionLog === "boolean"
      ? record.uploadSessionLog
      : DEFAULT_PREFERENCES.uploadSessionLog,
  };
}

/** Read the record once per module lifetime (tests reset it explicitly). */
export function loadPreferences(): UiPreferences {
  if (current !== null) return current;
  const store = storage();
  let parsed: unknown = null;
  const raw = store?.getItem(STORAGE_KEY);
  if (raw !== null && raw !== undefined) {
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = null;
    }
  } else {
    // One-time carry-over of the composer's older busy-Enter key.
    const legacy = store?.getItem(LEGACY_BUSY_ENTER_KEY);
    if (legacy === "queue" || legacy === "steer") parsed = { busyEnter: legacy };
  }
  current = normalizePreferences(parsed);
  return current;
}

/** Notify every consumer (dialog rows, composer, terminal) of a change. */
export function subscribePreferences(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Merge one update, persist it, and apply it to the document.
 * @param patch - fields to change; values are normalized like stored ones.
 * @returns the new record.
 */
export function updatePreferences(patch: Partial<UiPreferences>): UiPreferences {
  const next = normalizePreferences({ ...loadPreferences(), ...patch });
  current = next;
  try {
    storage()?.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* storage may be full or blocked; the in-memory record still applies */
  }
  applyPreferences(next);
  for (const listener of listeners) listener(next);
  return next;
}

/** Reset the in-memory cache so the next read reloads storage (tests). */
export function resetPreferencesForTests(): void {
  current = null;
  listeners.clear();
}

/** Match the built-in generic families that must stay unquoted. */
const GENERIC_FAMILIES = new Set([
  "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui",
  "ui-serif", "ui-sans-serif", "ui-monospace", "ui-rounded", "emoji", "math",
  "fangsong", "-apple-system", "blinkmacsystemfont",
]);

/**
 * Normalize a user-entered font list into valid CSS (upstream
 * `normalizeFontFamily`): drop quotes/backslashes/angle brackets/controls,
 * collapse whitespace, quote every non-generic name.
 */
export function normalizeFontFamily(value: string): string {
  return value
    .split(",")
    .map((name) => name.replace(/["'\\<>\u0000-\u001f\u007f]/gu, "").replace(/\s+/gu, " ").trim())
    .filter((name) => name !== "")
    .map((name) => (GENERIC_FAMILIES.has(name.toLowerCase()) ? name : `"${name}"`))
    .join(", ");
}

function systemPrefersLight(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(prefers-color-scheme: light)").matches;
}

/** The theme actually rendered for one preference. */
export function resolvedTheme(preference: ThemePreference): "light" | "dark" {
  if (preference === "system") return systemPrefersLight() ? "light" : "dark";
  return preference;
}

function setFontFamilyVariable(root: HTMLElement, name: string, value: string): void {
  const normalized = normalizeFontFamily(value);
  if (normalized === "") root.style.removeProperty(name);
  else root.style.setProperty(name, normalized);
}

/**
 * Apply the record to the document: resolved theme attribute, typography CSS
 * variables used by the conversation/code styles, and the terminal variables.
 */
export function applyPreferences(preferences: UiPreferences = loadPreferences()): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.dataset.theme = resolvedTheme(preferences.theme);
  root.dataset.themePreference = preferences.theme;
  root.style.setProperty("--content-font-size", `${preferences.fontSize}px`);
  root.style.setProperty("--code-font-size", `${preferences.codeFontSize}px`);
  root.style.setProperty("--terminal-font-size", `${preferences.terminalFontSize}px`);
  setFontFamilyVariable(root, "--font-family-text", preferences.textFontFamily);
  setFontFamilyVariable(root, "--font-family-code", preferences.codeFontFamily);
  setFontFamilyVariable(root, "--font-family-terminal", preferences.terminalFontFamily);
}

/**
 * Keep a `system` preference in step with the OS theme; returns a disposer.
 * Detached listeners are harmless in non-browser test environments.
 */
export function watchSystemTheme(): () => void {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => {};
  const query = window.matchMedia("(prefers-color-scheme: light)");
  const onChange = (): void => {
    if (loadPreferences().theme === "system") applyPreferences();
  };
  if (typeof query.addEventListener === "function") {
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }
  return () => {};
}
