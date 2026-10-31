import { webToken } from "./auth";

export interface OpenInAppEntry {
  id: string;
  label: string;
}

export async function fetchOpenInAppCatalog(
  fetchImpl: typeof fetch = fetch,
): Promise<OpenInAppEntry[]> {
  const token = webToken();
  const response = await fetchImpl("/open-in-app/apps", {
    method: "GET",
    headers: token ? { authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) {
    throw new Error(`open-in-app catalog failed: HTTP ${response.status}`);
  }
  const body = (await response.json()) as { apps?: unknown };
  if (!Array.isArray(body.apps)) return [];
  return body.apps.flatMap((entry) => {
    if (typeof entry !== "object" || entry === null) return [];
    const id = "id" in entry && typeof entry.id === "string" ? entry.id : "";
    const label = "label" in entry && typeof entry.label === "string" ? entry.label : id;
    return id === "" ? [] : [{ id, label }];
  });
}

export async function openInApp(
  app: string,
  path: string,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  const token = webToken();
  const response = await fetchImpl("/open-in-app/open", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ app, path }),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `open-in-app failed: HTTP ${response.status}`);
  }
}
