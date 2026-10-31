import { useCallback, useEffect, useState } from "react";
import { createDirectory, listDirectory, type DirectoryListResult } from "../wire/directoryPicker";

interface Props {
  open: boolean;
  initialPath?: string;
  onPick: (path: string) => void;
  onClose: () => void;
}

export function DirectoryPickerDialog({ open, initialPath, onPick, onClose }: Props) {
  const [view, setView] = useState<DirectoryListResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");

  const load = useCallback(async (path?: string) => {
    setError(null);
    try {
      setView(await listDirectory(path));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void load(initialPath);
  }, [initialPath, load, open]);

  if (!open) return null;

  return (
    <div
      className="directory-picker-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="directory-picker-dialog" role="dialog" aria-modal="true" aria-label="选择目录">
        <header className="directory-picker-header">
          <span>浏览目录</span>
          <button type="button" className="btn small ghost" onClick={onClose}>
            关闭
          </button>
        </header>
        {error !== null && (
          <div className="directory-picker-error" role="alert">
            {error}
          </div>
        )}
        {view !== null && (
          <>
            <nav className="directory-picker-crumbs" aria-label="路径">
              {view.crumbs.map((crumb) => (
                <button key={crumb.path} type="button" className="btn small ghost" onClick={() => void load(crumb.path)}>
                  {crumb.name}
                </button>
              ))}
            </nav>
            <ul className="directory-picker-entries">
              {view.entries.map((entry) => (
                <li key={entry.path}>
                  <button type="button" onClick={() => void load(entry.path)}>
                    {entry.name}
                  </button>
                </li>
              ))}
            </ul>
            <div className="directory-picker-actions">
              <input
                type="text"
                placeholder="新建文件夹名"
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
              />
              <button
                type="button"
                className="btn small"
                disabled={newName.trim() === ""}
                onClick={() => {
                  void createDirectory(view.path, newName.trim()).then(
                    () => {
                      setNewName("");
                      return load(view.path);
                    },
                    (caught: unknown) => {
                      setError(caught instanceof Error ? caught.message : String(caught));
                    },
                  );
                }}
              >
                新建
              </button>
              <button type="button" className="btn small primary" onClick={() => onPick(view.path)}>
                选择此目录
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
