// GoalBar: the goal indicator docked above the composer.
// Source: upstream packages/client/ui-goal/src/client/GoalBar.tsx —
// a present goal shows a phase label, the objective, and pause/resume/edit/clear.
// Loading, absent, and complete goals render nothing. Creation lives on `/goal`.

import { useCallback, useEffect, useRef, useState } from "react";
import type { GoalView } from "../wire/goals";

export interface GoalActionFailure {
  ok: false;
  error: { code: string; message: string };
}

export type GoalActionResult = { ok: true } | GoalActionFailure;

export interface GoalBarProps {
  /** Current goal; undefined = loading / capability absent, null = no goal. */
  goal: GoalView | null | undefined;
  onEdit: (objective: string) => Promise<GoalActionResult>;
  onPause: () => Promise<GoalActionResult>;
  onResume: () => Promise<GoalActionResult>;
  onClear: () => Promise<GoalActionResult>;
}

const PHASE_LABELS = {
  active: "进行中的目标",
  paused: "已暂停的目标",
  blocked: "受阻的目标",
} as const;

function phaseLabel(goal: GoalView): string {
  if (goal.phase === "active" && goal.activation === "disarmed") return "已暂停的目标";
  if (goal.phase === "active") return PHASE_LABELS.active;
  if (goal.phase === "paused") return PHASE_LABELS.paused;
  return PHASE_LABELS.blocked;
}

export function GoalBar({ goal, onEdit, onPause, onResume, onClear }: GoalBarProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [clearedGoalId, setClearedGoalId] = useState<string | null>(null);
  const pendingRef = useRef(false);

  const goalId = goal?.id;
  useEffect(() => {
    setEditing(false);
    setActionError(null);
    setClearedGoalId(null);
  }, [goalId]);

  const runAction = useCallback(async (action: () => Promise<GoalActionResult>) => {
    if (pendingRef.current) return undefined;
    pendingRef.current = true;
    setPending(true);
    setActionError(null);
    const result = await action();
    pendingRef.current = false;
    setPending(false);
    if (!result.ok) setActionError(`${result.error.message} (${result.error.code})`);
    return result;
  }, []);

  const handleEdit = useCallback(async () => {
    const trimmed = draft.trim();
    if (trimmed === "") return;
    const result = await runAction(() => onEdit(trimmed));
    if (result?.ok) setEditing(false);
  }, [draft, onEdit, runAction]);

  const handleClear = useCallback(
    async (clearedId: string) => {
      const result = await runAction(onClear);
      if (result?.ok) setClearedGoalId(clearedId);
    },
    [onClear, runAction],
  );

  if (goal === undefined || goal === null || goal.phase === "complete" || goal.id === clearedGoalId) {
    return null;
  }

  if (editing) {
    return (
      <div className="goal-dock" data-goal-bar>
        <div className="goal-bar goal-bar-edit">
          <input
            className="goal-editor"
            aria-label="目标内容"
            value={draft}
            disabled={pending}
            onChange={(event) => setDraft(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                void handleEdit();
              }
              if (event.key === "Escape") {
                event.preventDefault();
                setEditing(false);
              }
            }}
          />
          {actionError !== null && (
            <span className="goal-error" role="alert">
              {actionError}
            </span>
          )}
          <div className="goal-actions">
            <button
              type="button"
              className="btn small ghost"
              disabled={pending || draft.trim() === ""}
              aria-label="保存目标"
              onClick={() => void handleEdit()}
            >
              保存
            </button>
            <button
              type="button"
              className="btn small ghost"
              disabled={pending}
              aria-label="取消编辑"
              onClick={() => setEditing(false)}
            >
              取消
            </button>
          </div>
        </div>
      </div>
    );
  }

  const title = goal.phase === "blocked" ? goal.blockedReason?.message : undefined;
  const showResume = goal.phase === "paused" || (goal.phase === "active" && goal.activation === "disarmed");
  return (
    <div className="goal-dock" data-goal-bar>
      <div className="goal-bar" title={title}>
        <span className="goal-label">{phaseLabel(goal)}</span>
        <span className="goal-objective">{goal.objective}</span>
        {actionError !== null && (
          <span className="goal-error" role="alert">
            {actionError}
          </span>
        )}
        <div className="goal-actions">
          {goal.phase === "active" && goal.activation === "armed" && (
            <button
              type="button"
              className="btn small ghost"
              disabled={pending}
              aria-label="暂停目标"
              onClick={() => void runAction(onPause)}
            >
              暂停
            </button>
          )}
          {showResume && (
            <button
              type="button"
              className="btn small ghost"
              disabled={pending}
              aria-label="恢复目标"
              onClick={() => void runAction(onResume)}
            >
              恢复
            </button>
          )}
          <button
            type="button"
            className="btn small ghost"
            disabled={pending}
            aria-label="编辑目标"
            onClick={() => {
              setDraft(goal.objective);
              setEditing(true);
            }}
          >
            编辑
          </button>
          <button
            type="button"
            className="btn small ghost"
            disabled={pending}
            aria-label="清除目标"
            onClick={() => void handleClear(goal.id)}
          >
            清除
          </button>
        </div>
      </div>
    </div>
  );
}
