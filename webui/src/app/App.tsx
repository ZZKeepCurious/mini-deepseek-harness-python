import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useBackend } from "./useBackend";
import { useAccount } from "./useAccount";
import { useQuotaNotice } from "./useQuotaNotice";
import { usePreferences } from "./usePreferences";
import { useSettings } from "./useSettings";
import { usePermissionCatalog } from "./usePermissionCatalog";
import { useSchedules } from "./useSchedules";
import { useMessageFeedback } from "./useMessageFeedback";
import { loadScheduleHistory } from "../wire/schedule";
import {
  applyPreferences,
  loadPreferences,
  resolvedTheme,
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
import { permissionSelectionOf } from "../wire/permissions";
import { composerServices, executeCommand } from "../wire/composer";
import { foldPlanProjection, planSelectionOf } from "../wire/plan";
import { groupRows } from "../trajectory/model";
import type { ToolRow } from "../trajectory/model";
import { SessionList } from "../ui/SessionList";
import { SubagentPanel } from "../ui/SubagentPanel";
import { childSessionsOf, parentSessionOf, shortSessionId } from "../wire/subagents";
import { Trajectory } from "../ui/Trajectory";
import { ChatView } from "../ui/ChatView";
import { AccountMenu } from "../ui/AccountMenu";
import { AccountSection } from "../ui/AccountSection";
import { ApprovalPanel } from "../ui/ApprovalPanel";
import { PlanReviewPanel } from "../ui/PlanReviewPanel";
import { QuestionComposer } from "../ui/QuestionComposer";
import { planReviewOf } from "../wire/plan-review";
import { ControlPanel } from "../ui/ControlPanel";
import { SchedulePanel } from "../ui/SchedulePanel";
import { GoalBar } from "../ui/GoalBar";
import { useGoal } from "./useGoal";
import { DocumentPreview } from "../ui/DocumentPreview";
import { FilesPanel } from "../ui/FilesPanel";
import { QuotaNotice } from "../ui/QuotaNotice";
import { RightDock } from "../ui/RightDock";
import { SettingsDialog } from "../ui/SettingsDialog";
import type { SettingsSection } from "../ui/SettingsDialog";
import { Composer } from "../composer/Composer";
import { useModelCatalog } from "./useModelCatalog";
import { BrandSlot } from "../ui/BrandSlot";
import { DirectoryPickerDialog } from "../ui/DirectoryPickerDialog";
import { OpenInAppMenu } from "../ui/OpenInAppMenu";

const TerminalPanel = lazy(() =>
  import("../ui/TerminalPanel").then((module) => ({ default: module.TerminalPanel })),
);

type View = "chat" | "trajectory";

export function App() {
  const bk = useBackend();
  const settings = useSettings(bk.onEmit);
  const permissionCatalog = usePermissionCatalog(bk.onEmit);
  const schedules = useSchedules(bk.selectedId, bk.onEmit);
  const messageFeedback = useMessageFeedback(bk.selectedId);
  const modelCatalog = useModelCatalog(bk.selectedId);
  const { preferences, update: updatePreferences } = usePreferences();
  const colorScheme = resolvedTheme(preferences.theme);
  const account = useAccount({
    openStream: bk.openStream,
    subscribe: bk.onEmit,
    version: webuiPackage.version,
    colorScheme,
  });
  const quota = useQuotaNotice(bk.liveTrajectory);
  const goalChangeSeq = useMemo(() => {
    let seq = -1;
    for (const event of bk.trajectory) {
      if (event.type === "goal/change") seq = event.seq;
    }
    return seq;
  }, [bk.trajectory]);
  const goal = useGoal(bk.selectedId, goalChangeSeq);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsEntry, setSettingsEntry] = useState<SettingsSection | undefined>(undefined);
  const [view, setView] = useState<View>("chat");
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [directoryPickerOpen, setDirectoryPickerOpen] = useState(false);
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
  const selectedApproval =
    bk.selectedId === null
      ? null
      : bk.approvals.find((approval) => approval.agentId === bk.selectedId) ?? null;
  const selectedQuestion =
    bk.selectedId === null
      ? null
      : bk.questions.find((question) => question.agentId === bk.selectedId && question.hidden !== true) ?? null;
  const hiddenQuestion =
    bk.selectedId === null
      ? null
      : bk.questions.find((question) => question.agentId === bk.selectedId && question.hidden === true) ?? null;
  const selectedReview = selectedQuestion === null ? undefined : planReviewOf(selectedQuestion.questions);
  const approvalDetail = useMemo<ToolRow | null>(() => {
    const callId = selectedApproval?.callId;
    if (callId === undefined) return null;
    for (const row of groupRows(bk.trajectory)) {
      if (row.kind === "tool" && row.callId === callId) return row;
    }
    return null;
  }, [bk.trajectory, selectedApproval?.callId]);
  const pendingSessions = useMemo(
    () => new Set(bk.approvals.map((approval) => approval.agentId)),
    [bk.approvals],
  );
  const permission =
    bk.selectedId === null
      ? null
      : permissionSelectionOf(bk.projections[bk.selectedId]?.permissions);
  const plan =
    bk.selectedId === null
      ? null
      : planSelectionOf(bk.projections[bk.selectedId]?.plan) ??
        foldPlanProjection(bk.trajectory);

  const exitPlan = async (): Promise<string | null> => {
    if (bk.selectedId === null) return null;
    try {
      const result = await executeCommand(bk.selectedId, "/plan off", []);
      if (result === null) return "not a command";
      if (result.result.kind === "error") return result.result.text ?? "command refused";
      return null;
    } catch (error) {
      return error instanceof Error ? error.message : String(error);
    }
  };

  const columns = dockVisible && !dock.fullscreen
    ? `300px minmax(0, 1fr) ${dock.width}px`
    : "300px minmax(0, 1fr) 0";

  const subagentChildren =
    bk.selectedId === null ? [] : childSessionsOf(bk.selectedId, bk.sessions);
  const parentSession =
    bk.selectedId === null ? null : parentSessionOf(bk.selectedId, bk.sessions);
  const selectedSummary =
    bk.selectedId === null
      ? null
      : bk.sessions.find((session) => session.sessionId === bk.selectedId) ?? null;
  const feedbackEnabled = messageFeedback.error === null && !messageFeedback.loading;

  return (
    <div className="shell" style={{ gridTemplateColumns: columns }}>
      <div className="left-stack">
        <BrandSlot />
        <SessionList
          sessions={bk.sessions}
          selectedId={bk.selectedId}
          pending={pendingSessions}
          home={bk.host?.home}
          archivedIds={bk.workspaces.archivedSessionIds}
          workspaceRoster={bk.workspaces}
          showArchived={showArchived}
          createError={bk.error}
          onSelect={bk.selectSession}
          onCreate={bk.createSession}
          onBrowseDirectory={() => setDirectoryPickerOpen(true)}
          onFork={bk.forkSession}
          onArchive={bk.archiveSession}
          onUnarchive={bk.unarchiveSession}
          onShowArchived={setShowArchived}
          creating={false}
        />
        {bk.selectedId !== null && (
          <SubagentPanel
            parentSessionId={bk.selectedId}
            childSessions={subagentChildren}
            onOpen={bk.selectSession}
          />
        )}
      </div>

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
          {parentSession !== null && (
            <button
              type="button"
              className="btn small ghost"
              aria-label={`返回父会话 ${parentSession.sessionId}`}
              onClick={() => bk.selectSession(parentSession.sessionId)}
            >
              返回父会话 {shortSessionId(parentSession.sessionId)}
            </button>
          )}
          {bk.selectedId && (
            <>
              <span className="dim" style={{ fontFamily: "var(--mono)", fontSize: 11 }}>
                {bk.selectedId}
              </span>
              <OpenInAppMenu path={selectedSummary?.cwd} />
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
            <ChatView
              events={bk.trajectory}
              running={bk.running}
              blankSession={selectedSummary?.blank === true}
              historyHasMore={bk.hasMore}
              onLoadEarlier={() => void bk.loadEarlierHistory()}
              feedbackEnabled={feedbackEnabled}
              feedbackRating={messageFeedback.ratingOf}
              onFeedbackRate={(messageId, rating) => void messageFeedback.setRating(messageId, rating)}
              onFeedbackClear={(messageId) => void messageFeedback.clearRating(messageId)}
            />
          ) : (
            <Trajectory
              events={bk.trajectory}
              running={bk.running}
              historyHasMore={bk.hasMore}
              onLoadEarlier={() => void bk.loadEarlierHistory()}
            />
          )}
        </div>
        {selectedApproval !== null ? (
          <ApprovalPanel
            approval={selectedApproval}
            detail={approvalDetail}
            onAnswer={bk.askApproval}
          />
        ) : selectedQuestion !== null && selectedReview !== undefined ? (
          <PlanReviewPanel
            pendingKey={selectedQuestion.key}
            review={selectedReview}
            onAnswer={(answer) => bk.answerQuestion(selectedQuestion, answer)}
            onDismiss={() => bk.dismissQuestion(selectedQuestion)}
          />
        ) : selectedQuestion !== null ? (
          <QuestionComposer
            pendingKey={selectedQuestion.key}
            questions={selectedQuestion.questions}
            onAnswer={(answer) => bk.answerQuestion(selectedQuestion, answer)}
            onDismiss={() => bk.dismissQuestion(selectedQuestion)}
            clock={selectedQuestion.clock}
            onTakeTime={() => { bk.takeTimeQuestion(selectedQuestion); }}
            onHoldFocus={() => { bk.holdQuestionFocus(selectedQuestion); }}
            onReleaseFocus={() => { bk.releaseQuestionFocus(selectedQuestion); }}
            onEngage={() => { bk.engageQuestion(selectedQuestion); }}
          />
        ) : (
          <>
            {hiddenQuestion !== null && (
              <div className="question-hidden-seat">
                <button
                  type="button"
                  className="btn"
                  onClick={() => { bk.revealQuestion(hiddenQuestion); }}
                >
                  打开提问
                </button>
              </div>
            )}
            <GoalBar
              goal={goal.goal}
              onEdit={goal.edit}
              onPause={goal.pause}
              onResume={goal.resume}
              onClear={goal.clear}
            />
            <Composer
              sessionId={bk.selectedId}
              running={bk.running}
              services={composerServices}
              permission={permission}
              permissionCatalog={permissionCatalog.catalog}
              plan={plan}
              onExitPlan={exitPlan}
              modelCatalog={modelCatalog.catalog}
              modelSelection={modelCatalog.selection}
              onSelectModel={(provider, model) =>
                bk.selectedId === null
                  ? Promise.resolve("缺少会话")
                  : modelCatalog.select(bk.selectedId, provider, model)
              }
            />
          </>
        )}
      </div>

      {dockVisible && (
        <RightDock
          layout={dock}
          onView={setViewAndPersist}
          onWidth={resizeDock}
          onCollapse={collapseDock}
          onToggleFullscreen={toggleFullscreen}
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
          ) : dock.view === "schedule" ? (
            <SchedulePanel
              sessionId={bk.selectedId}
              schedules={schedules.schedules}
              catalog={schedules.catalog}
              loading={schedules.loading}
              catalogLoading={schedules.catalogLoading}
              error={schedules.error}
              catalogError={schedules.catalogError}
              onRefresh={() => void schedules.reload()}
              onRefreshCatalog={() => void schedules.reloadCatalog()}
              onDelete={(id, ownerSessionId) => void schedules.remove(id, ownerSessionId)}
              onUpdate={(id, expected, patch, ownerSessionId) => {
                void schedules.update(id, expected, patch, ownerSessionId);
              }}
              onLoadHistory={(scheduleId, ownerSessionId) => {
                const sid = ownerSessionId ?? bk.selectedId;
                if (sid === null) return Promise.resolve([]);
                return loadScheduleHistory(sid, scheduleId);
              }}
            />
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
              onJobKill={(jobId) => void bk.killJob(jobId)}
              onJobPreview={(jobId) => bk.previewJobOutput(jobId)}
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
        <AccountMenu
          account={account}
          colorScheme={colorScheme}
          settingsOpen={settingsOpen}
          onOpenSettings={() => {
            setSettingsEntry(undefined);
            setSettingsOpen(true);
          }}
          onAddApiKey={() => {
            setSettingsEntry("plugins");
            setSettingsOpen(true);
          }}
        />
        <button
          type="button"
          className="btn small ghost settings-open"
          aria-label="设置"
          onClick={() => {
            setSettingsEntry(undefined);
            setSettingsOpen(true);
          }}
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
        accountSection={
          account.view?.status === "credential-stored" ? (
            <AccountSection account={account} colorScheme={colorScheme} />
          ) : undefined
        }
        initialSection={settingsEntry}
        modelCatalog={modelCatalog}
      />
      {quota.notice !== null && (
        <QuotaNotice
          code={quota.notice.code}
          signedIn={account.view?.status === "credential-stored"}
          topUpUrl={account.view?.links.topUpUrl}
          onDismiss={quota.dismiss}
        />
      )}
      <DirectoryPickerDialog
        open={directoryPickerOpen}
        initialPath={bk.host?.home}
        onClose={() => setDirectoryPickerOpen(false)}
        onPick={(path) => {
          setDirectoryPickerOpen(false);
          void bk.createSession(path);
        }}
      />
    </div>
  );
}
