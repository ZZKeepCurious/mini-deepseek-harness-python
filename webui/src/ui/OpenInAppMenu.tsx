import { useCallback, useEffect, useState } from "react";
import { fetchOpenInAppCatalog, openInApp, type OpenInAppEntry } from "../wire/openInApp";

interface Props {
  path: string | null | undefined;
}

export function OpenInAppMenu({ path }: Props) {
  const [apps, setApps] = useState<OpenInAppEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void fetchOpenInAppCatalog()
      .then((rows) => {
        if (!cancelled) setApps(rows);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setApps([]);
          setError(caught instanceof Error ? caught.message : String(caught));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const runOpen = useCallback(
    async (appId: string) => {
      if (path === null || path === undefined || path === "") return;
      setOpening(true);
      setError(null);
      try {
        await openInApp(appId, path);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setOpening(false);
      }
    },
    [path],
  );

  if (apps === null || apps.length === 0) return null;
  if (path === null || path === undefined || path === "") return null;

  return (
    <span className="open-in-app-menu">
      {apps.map((app) => (
        <button
          key={app.id}
          type="button"
          className="btn small ghost"
          disabled={opening}
          aria-label={`在 ${app.label} 中打开工作目录`}
          onClick={() => void runOpen(app.id)}
        >
          {app.label}
        </button>
      ))}
      {error !== null && (
        <span className="open-in-app-error dim" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}
