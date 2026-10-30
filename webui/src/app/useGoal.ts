// Current-session goal: `goals/get` plus CAS mutations.
// Activation is process-local and only appears on the host view, so the bar
// reads the Remote instead of the durable projection snapshot.

import { useCallback, useEffect, useState } from "react";
import { RpcFailure } from "../wire";
import {
  clearGoal,
  editGoal,
  getGoal,
  goalRefOf,
  pauseGoal,
  resumeGoal,
  type GoalView,
} from "../wire/goals";
import type { GoalActionResult } from "../ui/GoalBar";

export interface UseGoal {
  goal: GoalView | null | undefined;
  edit: (objective: string) => Promise<GoalActionResult>;
  pause: () => Promise<GoalActionResult>;
  resume: () => Promise<GoalActionResult>;
  clear: () => Promise<GoalActionResult>;
}

function failureOf(error: unknown): GoalActionResult {
  if (error instanceof RpcFailure) {
    return { ok: false, error: { code: error.code, message: error.message.replace(/^[^:]+:\s*/, "") } };
  }
  return {
    ok: false,
    error: {
      code: "gateway/internal",
      message: error instanceof Error ? error.message : String(error),
    },
  };
}

export function useGoal(sessionId: string | null, changeSeq = -1): UseGoal {
  const [goal, setGoal] = useState<GoalView | null | undefined>(undefined);

  const reload = useCallback(async () => {
    if (sessionId === null) {
      setGoal(undefined);
      return;
    }
    try {
      setGoal(await getGoal(sessionId));
    } catch (error) {
      if (error instanceof RpcFailure && error.code === "gateway/invocation-unavailable") {
        setGoal(undefined);
        return;
      }
      setGoal(null);
    }
  }, [sessionId, changeSeq]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const mutate = useCallback(
    async (run: (current: GoalView) => Promise<GoalView | void>): Promise<GoalActionResult> => {
      if (sessionId === null || goal === undefined || goal === null) {
        return { ok: false, error: { code: "no-current-goal", message: "No current goal" } };
      }
      try {
        const next = await run(goal);
        if (next === undefined) {
          setGoal(null);
        } else {
          setGoal(next);
        }
        return { ok: true };
      } catch (error) {
        return failureOf(error);
      }
    },
    [goal, sessionId],
  );

  return {
    goal: sessionId === null ? undefined : goal,
    edit: (objective) =>
      mutate((current) => editGoal(sessionId as string, goalRefOf(current), { objective })),
    pause: () => mutate((current) => pauseGoal(sessionId as string, goalRefOf(current))),
    resume: () => mutate((current) => resumeGoal(sessionId as string, goalRefOf(current))),
    clear: () =>
      mutate(async (current) => {
        await clearGoal(sessionId as string, goalRefOf(current));
      }),
  };
}
