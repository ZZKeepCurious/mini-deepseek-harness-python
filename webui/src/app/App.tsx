import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useBackend } from "./useBackend";
import { usePreferences } from "./usePreferences";
import { useSettings } from "./useSettings";
import {
  applyPreferences,
  loadPreferences,
  watchSystemTheme,
} from "./uiPreferences";
import webuiPackage from "../../package.json";
import {
  clampDockWidth,
  defaultDockLayout,
  loadDockLayout,
  narrowViewport,
  saveDockLayout,
  type DockLayout,
  type RightView,
} from "./rightDock";
import { inboxQueue } from "../wire/control";
import { composerServices } from "../wire/composer";
import { SessionList } from "../ui/SessionList";
import { Trajectory } from "../ui/Trajectory";
import { ChatView } from "../ui/ChatView";
import { ApprovalPanel } from "../ui/ApprovalPanel";
import { ControlPanel } from "../ui/ControlPanel";
import { DocumentPreview } from "../ui/DocumentPreview";
import { FilesPanel } from "../ui/FilesPanel";
import { RightDock } from "../ui/RightDock";
import { SettingsDialog } from "../ui/SettingsDialog";
import { Composer } from "../composer/Composer";

const TerminalPanel = lazy(() =>
  import("../ui/TerminalPanel").then((module) => ({ default: module.TerminalPanel })),
);

type View = "chat" | "trajectory";

export function App() {
  const bk = useBackend();
  const settings = useSettings(bk.onEmit);
  const { preferences, update: updatePreferences } = usePreferences();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [view, setView] = useState<View>("chat");
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [dock, setDock] = useState<DockLayout>(() => defaultDockLayout());
  const dockRef = useRef(dock);
  dockRef.current = dock;

  useEffect(() => {
    applyPreferences(loadPreferences());
    return watchSystemTheme();
  }, []);

  useEffect(() => {
    applyPreferences(preferences);
  }, [preferences]);

  useEffect(() => {
    if (!preferences.showCodingView && view === "trajectory") setView("chat");
  }, [preferences.showCodingView, view]);

  useEffect(() => {
    setPreviewPath(null);
  }, [bk.selectedId]);

  useEffect(() => {
    const sessionId = bk.selectedId;
    setDock(sessionId === null ? defaultDockLayout() : loadDockLayout(sessionId));
    return () => {
      saveDockLayout(sessionId, dockRef.current);
    };
  }, [bk.selectedId]);

  useEffect(() => {
    const save = (): void => {
      saveDockLayout(bk.selectedId, dockRef.current);
    };
    window.addEventListener("beforeunload", save);
    return () => window.removeEventListener("beforeunload", save);
  }, [bk.selectedId]);

  const dockVisible = bk.selectedId !== null && !dock.collapsed;

  const setViewAndPersist = (next: RightView): void => {
    setDock((current) => ({ ...current, view: next }));
  };

  const resizeDock = (width: number): void => {
    setDock((current) => ({ ...current, width: clampDockWidth(width) }));
  };

  const openDock = (): void => {
    setDock((current) => ({
      ...current,
      collapsed: false,
      fullscreen: narrowViewport(),
    }));
  };

  const collapseDock = (): void => {
    setDock((current) => ({ ...current, collapsed: true, fullscreen: false }));
  };

  const toggleFullscreen = (): void => {
    setDock((current) => {
      if (current.fullscreen) {
        if (narrowViewport()) return { ...current, collapsed: true, fullscreen: false };
        return { ...current, fullscreen: false };
      }
      return { ...current, fullscreen: true };
    });
  };

  const queue = bk.selectedId ? inboxQueue(bk.projections[bk.selectedId]?.inbox) : [];
  const jobs = bk.selectedId ? bk.jobs[bk.selectedId] ?? [] : [];

  const columns = dockVisible && !dock.fullscreen
    ? `300px minmax(0, 1fr) ${dock.width}px`
    : "300px minmax(0, 1fr) 0";

  return (
    <div className="shell" style={{ gridTemplateColumns: columns }}>
      <SessionList
        sessions={bk.sessions}
        selectedId={bk.selectedId}
        onSelect={bk.selectSession}
        onCreate={bk.createSession}
        creating={false}
      />

      <div className="panel center">
        <div className="panel-header">
          <div className="view-tabs" role="tablist" aria-label="会话视图">
            <button
              type="button"
              role="tab"
              aria-selected={view === "chat"}
              className={`view-tab${view === "chat" ? " active" : ""}`}
              onClick={() => setView("chat")}
            >
              Chat
            </button>
            {preferences.showCodingView && (
              <button
                type="button"
                role="tab"
                aria-selected={view === "trajectory"}
                className={`view-tab${view === "trajectory" ? " active" : ""}`}
                onClick={() => setView("trajectory")}
              >
                Trajectory
              </button>
            )}
          </div>
          <span className="spacer" />
          {bk.selectedId && (
            <>
              <span className="dim" style={{ fontFamily: "var(--mono)", fontSize: 11 }}>
                {bk.selectedId}
              </span>
              <button className="btn small ghost" onClick={() => bk.refresh()}>
                刷新
              </button>
              {!dockVisible && (
                <button
                  className="btn small ghost dock-expand"
                  onClick={openDock}
                  aria-label="展开右侧栏"
                  title="展开右侧栏"
                >
                  侧栏
                </button>
              )}
            </>
          )}
        </div>
        <div className="panel-body center-body">
          {bk.trajectory.length === 0 && !bk.selectedId && (
            <div className="empty">选择或新建一个会话开始</div>
          )}
          {previewPath !== null && bk.selectedId !== null ? (
            <DocumentPreview
              sessionId={bk.selectedId}
              path={previewPath}
              services={bk.workspace}
              onClose={() => setPreviewPath(null)}
            />
          ) : view === "chat" ? (
            <ChatView events={bk.trajectory} running={bk.running} />
          ) : (
            <Trajectory events={bk.trajectory} running={bk.running} />
          )}
        </div>
        <Composer
          sessionId={bk.selectedId}
          running={bk.running}
          services={composerServices}
        />
      </div>

      {dockVisible && (
        <RightDock
          layout={dock}
          onView={setViewAndPersist}
          onWidth={resizeDock}
          onCollapse={collapseDock}
          onToggleFullscreen={toggleFullscreen}
          top={
            <ApprovalPanel
              approvals={bk.approvals}
              sessionId={bk.selectedId}
              onResolve={(a, allowed) => bk.askApproval(a, allowed, bk.selectedId ?? "")}
            />
          }
        >
          {dock.view === "files" ? (
            <FilesPanel
              sessionId={bk.selectedId}
              services={bk.workspace}
              onOpen={setPreviewPath}
            />
          ) : dock.view === "terminal" ? (
            <Suspense fallback={<div className="empty">加载终端…</div>}>
              <TerminalPanel sessionId={bk.selectedId} services={bk.terminal} />
            </Suspense>
          ) : (
            <ControlPanel
              queue={queue}
              jobs={jobs}
              running={bk.running}
              sessionId={bk.selectedId}
              onQueueEdit={(itemId, text) =>
                void bk.updateQueue(itemId, { kind: "edit", content: [{ type: "text", text }] })
              }
              onQueueRemove={(itemId) => void bk.updateQueue(itemId, { kind: "remove" })}
              onQueueSteer={(itemId) => void bk.updateQueue(itemId, { kind: "steer" })}
            />
          )}
        </RightDock>
      )}

      <div className="statusbar" style={{ gridColumn: "1 / -1" }}>
        {bk.ready && bk.host ? (
          <span>client {bk.host.clientId} · home {bk.host.home}</span>
        ) : (
          <span>connecting…</span>
        )}
        <span>{bk.connected ? "已连接" : "未连接"}</span>
        <span>{bk.sessions.length} 会话</span>
        <button
          type="button"
          className="btn small ghost settings-open"
          aria-label="设置"
          onClick={() => setSettingsOpen(true)}
        >
          设置
        </button>
        {bk.error && <span className="err" style={{ color: "var(--err)" }}>{bk.error}</span>}
      </div>

      <SettingsDialog
        open={settingsOpen}
        settings={settings}
        preferences={preferences}
        onPreference={updatePreferences}
        onClose={() => setSettingsOpen(false)}
        version={webuiPackage.version}
      />
    </div>
  );
}
