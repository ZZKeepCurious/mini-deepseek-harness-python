// Display a host path with the home directory abbreviated as `~`.
// Source: packages/client/ui-session-list home-path-tilde scene.

export function displayHomePath(home: string | undefined, path: string | undefined): string {
  if (path === undefined || path === "") return "";
  if (home === undefined || home === "") return path;
  const normHome = home.replace(/[\\/]+$/, "").split("\\").join("/");
  const normPath = path.split("\\").join("/");
  if (normPath === normHome) return "~";
  if (normPath.startsWith(`${normHome}/`)) return `~${normPath.slice(normHome.length)}`;
  return path;
}
