// Optional web token (backend MINIHARNESS_WEB_TOKEN). The browser can only
// carry it via `?token=` on the page URL: unary calls send it as an
// `Authorization: Bearer` header, the WebSocket appends it to the mux URL.
// Source: miniharness/web/auth.py (upstream connection.requestRejection).

let cached: string | null | undefined;

/** Read the token once from the page URL (`?token=<t>`); null when absent. */
export function webToken(): string | null {
  if (cached !== undefined) return cached;
  try {
    const search =
      typeof globalThis.location === "object" && globalThis.location
        ? globalThis.location.search
        : "";
    cached = new URLSearchParams(search || "").get("token");
  } catch {
    cached = null;
  }
  return cached;
}

/** Test hook: forget the cached token so the next webToken() re-reads location. */
export function resetWebTokenCache(): void {
  cached = undefined;
}
