import { useMemo, useState } from "react";
import { planSummaryOf, type PlanReview } from "../wire/plan-review";
import type { QuestionAnswer } from "../wire/questions";

export interface PlanReviewPanelProps {
  pendingKey: string;
  review: PlanReview;
  onAnswer: (answer: QuestionAnswer) => Promise<void>;
  onDismiss: () => Promise<void>;
}

function tooltip(description: string | undefined): { title?: string } {
  return description === undefined ? {} : { title: description };
}

export function PlanReviewPanel({ pendingKey, review, onAnswer, onDismiss }: PlanReviewPanelProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const summary = useMemo(() => planSummaryOf(review.plan), [review.plan]);

  const settle = (send: () => Promise<void>): void => {
    if (busy) return;
    setBusy(true);
    setError(null);
    void send()
      .then(() => undefined)
      .catch((cause: unknown) => {
        setBusy(false);
        setError(cause instanceof Error ? cause.message : String(cause));
      });
  };

  return (
    <div className="plan-review" data-plan-review-key={pendingKey}>
      <section className="plan-review-card" aria-label={review.question} aria-busy={busy}>
        <div className="plan-review-strip">
          <span className={`plan-review-dot${busy ? " ongoing" : ""}`} data-state={busy ? "ongoing" : "warning"} />
          计划审查
        </div>
        <div className="plan-review-summary">
          <h3 className="plan-review-title">{summary.title}</h3>
          {summary.description !== "" && <p className="plan-review-description">{summary.description}</p>}
        </div>
        <div className="plan-review-footer">
          <div className="plan-review-feedback" role="status">{error}</div>
          <div className="plan-review-actions">
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => { settle(() => onDismiss()); }}
            >
              讨论修改
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={busy}
              {...tooltip(review.approve.description)}
              onClick={() => {
                settle(() => onAnswer({ answers: [{ id: review.id, selected: [review.approve.label] }] }));
              }}
            >
              批准
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
