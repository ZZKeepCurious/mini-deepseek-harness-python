// Authorization link decoration (upstream
// ui-settings-account/src/client/authorize-url.ts): the Platform login pages
// follow the palette of the active application theme.

/** @param authorizeUrl - Host authorization URL. @param colorScheme - resolved scheme of the active theme. */
export function authorizeUrlWithTheme(
  authorizeUrl: string,
  colorScheme: "light" | "dark",
): string {
  const url = new URL(authorizeUrl);
  url.searchParams.set("theme", colorScheme);
  return url.href;
}
