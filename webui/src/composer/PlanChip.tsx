// Plan-mode chip over the host-computed `plan` projection.
// Source: upstream packages/client/ui-plan/src/client/PlanModeControl.tsx —
// renders only while the effective target is plan mode
// (`pending ? !active : active`) and executes /plan off.

import { useEffect, useRef, useState } from "react";
import { planChipTarget, type PlanSelection } from "../wire/plan";

export interface PlanChipProps {
  plan: PlanSelection | null;
  locked: boolean;
  /** Turn plan mode off. Return a failure string to show inline; null on success. */
  onExit: () => Promise<string | null>;
}

export function PlanChip({ plan, locked, onExit }: PlanChipProps) {
  const [leaving, setLeaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  if (plan === null || !planChipTarget(plan)) return null;

  const off = (): void => {
    setLeaving(true);
    setError(null);
    void onExit().then(
      (failure) => {
        if (!aliveRef.current) return;
        setLeaving(false);
        setError(failure);
      },
      (reason: unknown) => {
        if (!aliveRef.current) return;
        setLeaving(false);
        setError(reason instanceof Error ? reason.message : String(reason));
      },
    );
  };

  return (
    <span className="plan-chip-wrap">
      <button
        type="button"
        className="btn small ghost plan-chip"
        aria-label="计划模式已开启，按下关闭"
        title="计划模式已开启 — 点击关闭（/plan off）"
        disabled={locked || leaving}
        onClick={off}
      >
        计划
      </button>
      {error !== null && (
        <span className="plan-chip-error" role="status" title={error}>
          退出计划模式失败
        </span>
      )}
    </span>
  );
}
