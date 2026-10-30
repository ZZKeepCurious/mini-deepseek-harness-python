// Approval takeover for one pending `approval/request` waterfall.
// Source: upstream packages/client/ui-approval/src/client/ApprovalPanel.tsx —
// the composer seat shows the request reason (localized copy first), the
// correlated tool detail, and answers Enter = allow once / Escape = reject.

import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import type { PendingApproval } from "../app/useBackend";
import type { ApprovalOutcome } from "../app/useBackend";
import type { ToolRow } from "../trajectory/model";
import { ToolCallView } from "./tools/ToolCallView";

interface Props {
  approval: PendingApproval;
  /** Tool row correlated through `callId`; null when the call is not in the followed history. */
  detail: ToolRow | null;
  onAnswer: (approval: PendingApproval, outcome: ApprovalOutcome) => Promise<void>;
}

const IGNORED_TARGETS =
  'input, textarea, select, [contenteditable="true"], [contenteditable=""]';

function reasonText(approval: PendingApproval): string | null {
  const display = approval.displayReason;
  if (display !== undefined) {
    const zh = display.zh;
    if (typeof zh === "string" && zh !== "") return zh;
    const en = display.en;
    if (typeof en === "string" && en !== "") return en;
  }
  return approval.reason ?? null;
}

export function ApprovalPanel({ approval, detail, onAnswer }: Props) {
  const [answered, setAnswered] = useState(false);
  const waiting = useRef(false);
  const active = useRef(true);
  const composing = useRef(false);
  const compositionEnded = useRef(false);

  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);

  const answer = (outcome: ApprovalOutcome): void => {
    if (waiting.current) return;
    waiting.current = true;
    setAnswered(true);
    void onAnswer(approval, outcome).catch(() => {
      if (!active.current) return;
      waiting.current = false;
      setAnswered(false);
    });
  };

  const keydown = (event: KeyboardEvent<HTMLDivElement>): void => {
    const element = event.target as Element;
    if (
      event.defaultPrevented ||
      !event.currentTarget.contains(document.activeElement) ||
      element.closest(IGNORED_TARGETS) !== null
    ) {
      return;
    }
    if (event.key !== "Enter" && event.key !== "Escape") return;
    if (
      event.key === "Enter" &&
      element.closest('button, a[href], [role="button"]') !== null
    ) {
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    event.stopPropagation();
    if (
      event.repeat ||
      composing.current ||
      compositionEnded.current ||
      event.nativeEvent.isComposing ||
      event.nativeEvent.keyCode === 229
    ) {
      return;
    }
    answer(event.key === "Enter" ? "allowed-once" : "rejected");
  };

  const reason = reasonText(approval);

  return (
    <div
      className="approval-takeover"
      data-approval-key={approval.eventId}
      aria-busy={answered}
      onKeyDown={keydown}
      onKeyUpCapture={() => {
        compositionEnded.current = false;
      }}
      onCompositionStartCapture={() => {
        composing.current = true;
      }}
      onCompositionEndCapture={() => {
        composing.current = false;
        compositionEnded.current = true;
      }}
    >
      <div className="approval-card">
        <div className="approval-strip">
          <span
            className={`approval-dot ${answered ? "ongoing" : "warning"}`}
            data-state={answered ? "ongoing" : "warning"}
            aria-hidden
          />
          <span>等待审批</span>
        </div>
        <div
          className="approval-body"
          role="group"
          aria-label="审批详情"
          tabIndex={0}
        >
          <div className="approval-headline">
            {reason ?? `工具 ${approval.toolName || "(未知工具)"} 请求越权执行`}
          </div>
          {detail !== null && (
            <div className="approval-detail">
              <ToolCallView row={detail} terms={[]} />
            </div>
          )}
        </div>
        <div className="approval-actions">
          <button
            type="button"
            className="btn small ghost"
            disabled={answered}
            onClick={() => answer("rejected")}
          >
            拒绝
          </button>
          <button
            type="button"
            className="btn small"
            disabled={answered}
            onClick={() => answer("allowed-once")}
          >
            允许一次
          </button>
        </div>
      </div>
    </div>
  );
}
