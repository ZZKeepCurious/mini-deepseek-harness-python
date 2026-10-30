import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, KeyboardEvent } from "react";
import { MarkdownText } from "./MarkdownText";
import type { QuestionAnswer, QuestionItem } from "../wire/questions";
import {
  answeredDraft,
  completedDraft,
  encodeQuestionAnswers,
  initialQuestionDrafts,
  parseRecommendedLabel,
  readQuestionDraft,
  writeQuestionDraft,
  clearQuestionDraft,
  type QuestionDraftAnswer,
} from "../wire/question-draft";
import {
  questionTakeTimeVisible,
  questionWaitStatus,
  type QuestionClock,
} from "../wire/question-wait";

export interface QuestionComposerProps {
  pendingKey: string;
  questions: QuestionItem[];
  onAnswer: (answer: QuestionAnswer) => Promise<void>;
  onDismiss: () => Promise<void>;
  clock?: QuestionClock;
  onTakeTime?: () => void;
  onHoldFocus?: () => void;
  onReleaseFocus?: () => void;
  onEngage?: () => void;
}

const COPY = {
  incomplete: "请先完成这道问题。",
  unanswered: "请选择一个选项或填写自定义答案。",
  prev: "上一题",
  next: "下一题",
  minimize: "收起问题卡片",
  maximize: "展开问题卡片",
  cancel: "放弃整组问题",
  recommended: "推荐",
  placeholder: "输入你的答案",
  skip: "跳过",
  submit: "提交",
  submitting: "正在提交…",
  actionNext: "下一题",
  takeTime: "慢慢回答",
  hide: "收起问题面板，可从工具调用重新打开",
};

function isComposing(event: KeyboardEvent<HTMLTextAreaElement>): boolean {
  return event.nativeEvent.isComposing || Reflect.get(event.nativeEvent, "keyCode") === 229;
}

function loadProgress(pendingKey: string, questions: readonly QuestionItem[]) {
  return readQuestionDraft(pendingKey, questions.length)
    ?? { index: 0, drafts: initialQuestionDrafts(questions) };
}

function AnswerField(props: {
  variant: "inline" | "block";
  value: string;
  disabled: boolean;
  autoFocus?: boolean;
  onFocus?: () => void;
  onChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
}) {
  return (
    <div className={`question-field ${props.variant === "inline" ? "inline" : "block"}`}>
      <div aria-hidden className="question-field-mirror">{`${props.value}\n`}</div>
      <textarea
        autoFocus={props.autoFocus}
        className="question-field-input"
        value={props.value}
        disabled={props.disabled}
        rows={1}
        placeholder={COPY.placeholder}
        onFocus={props.onFocus}
        onChange={props.onChange}
        onKeyDown={props.onKeyDown}
      />
    </div>
  );
}

export function QuestionComposer({
  pendingKey,
  questions,
  onAnswer,
  onDismiss,
  clock,
  onTakeTime,
  onHoldFocus,
  onReleaseFocus,
  onEngage,
}: QuestionComposerProps) {
  const loaded = loadProgress(pendingKey, questions);
  const [index, setIndex] = useState(loaded.index);
  const [drafts, setDrafts] = useState(loaded.drafts);
  const [busy, setBusy] = useState<"answer" | "cancel" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [minimized, setMinimized] = useState(false);
  const focusedQuestions = useRef(new Set<number>());
  const requestKey = useRef(pendingKey);

  useEffect(() => {
    if (requestKey.current === pendingKey) return;
    requestKey.current = pendingKey;
    const next = loadProgress(pendingKey, questions);
    setIndex(next.index);
    setDrafts(next.drafts);
    setBusy(null);
    setError(null);
    setMinimized(false);
    focusedQuestions.current = new Set();
  }, [pendingKey, questions]);

  const replaceProgress = (nextIndex: number, nextDrafts: QuestionDraftAnswer[]): void => {
    writeQuestionDraft(pendingKey, { index: nextIndex, drafts: nextDrafts });
    setIndex(nextIndex);
    setDrafts(nextDrafts);
  };

  const question = questions[index];
  const draft = drafts[index];
  const hasOptions = (question?.options?.length ?? 0) > 0;
  const canSubmit = clock === undefined || clock.channel !== "none";
  const locked = busy !== null;
  const dismissLabel = clock?.dismissal === "hide" ? COPY.hide : COPY.cancel;
  const waitLabel = clock === undefined
    ? undefined
    : clock.waitState === "continued" || clock.waitState === "waiting" || clock.waitState === "editing"
      || clock.remainingMs !== undefined
      ? questionWaitStatus(clock.waitState, clock.remainingMs ?? 0)
      : undefined;
  const showTakeTime = clock !== undefined
    && onTakeTime !== undefined
    && questionTakeTimeVisible(clock.waitState, clock.remainingMs !== undefined);

  const updateDraft = (
    update: (current: QuestionDraftAnswer) => QuestionDraftAnswer,
    nextIndex = index,
  ): void => {
    const nextDrafts = drafts.map((item, itemIndex) => (itemIndex === index ? update(item) : item));
    replaceProgress(nextIndex, nextDrafts);
    setError(null);
    onEngage?.();
  };

  const choose = (label: string): void => {
    if (question === undefined) return;
    updateDraft((current) => {
      if (question.multiSelect === true) {
        const selected = current.selected.includes(label)
          ? current.selected.filter((item) => item !== label)
          : [...current.selected, label];
        return { ...current, selected, skipped: false };
      }
      return { selected: [label], custom: "", skipped: false };
    }, question.multiSelect !== true && index < questions.length - 1 ? index + 1 : index);
  };

  const submitDrafts = (values: QuestionDraftAnswer[]): void => {
    const missing = values.findIndex((item) => !completedDraft(item));
    if (missing >= 0) {
      replaceProgress(missing, values);
      setError(COPY.incomplete);
      return;
    }
    if (!canSubmit) return;
    setBusy("answer");
    setError(null);
    void onAnswer(encodeQuestionAnswers(questions, values))
      .then(() => { clearQuestionDraft(pendingKey); })
      .catch((cause: unknown) => {
        setBusy(null);
        setError(cause instanceof Error ? cause.message : String(cause));
      });
  };

  const continueFlow = (): void => {
    if (draft === undefined || !answeredDraft(draft)) {
      setError(COPY.unanswered);
      return;
    }
    if (index < questions.length - 1) {
      replaceProgress(index + 1, drafts);
      setError(null);
      return;
    }
    submitDrafts(drafts);
  };

  const draftCustom = (event: ChangeEvent<HTMLTextAreaElement>): void => {
    const value = event.target.value;
    updateDraft((current) => ({
      ...current,
      selected: question?.multiSelect === true ? current.selected : [],
      custom: value,
      skipped: false,
    }));
  };

  const continueFromCustom = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key !== "Enter" || event.shiftKey || isComposing(event)) return;
    event.preventDefault();
    continueFlow();
  };

  const skipQuestion = (): void => {
    const nextDrafts = drafts.map((item, itemIndex) => (
      itemIndex === index ? { selected: [], custom: "", skipped: true } : item
    ));
    replaceProgress(index < questions.length - 1 ? index + 1 : index, nextDrafts);
    setError(null);
    if (index < questions.length - 1) return;
    submitDrafts(nextDrafts);
  };

  const dismissFlow = (): void => {
    if (busy !== null) return;
    setBusy("cancel");
    setError(null);
    void onDismiss()
      .then(() => { clearQuestionDraft(pendingKey); })
      .catch((cause: unknown) => {
        setBusy(null);
        setError(cause instanceof Error ? cause.message : String(cause));
      });
  };

  if (question === undefined || draft === undefined) {
    return (
      <div className="question-composer" data-question-key={pendingKey}>
        <section className="question-card">
          <header className="question-header">
            <button type="button" className="btn ghost" aria-label={dismissLabel} onClick={dismissFlow}>
              {dismissLabel}
            </button>
          </header>
        </section>
      </div>
    );
  }

  return (
    <div className="question-composer" data-question-key={pendingKey}>
      <section
        className={`question-card${minimized ? " minimized" : ""}`}
        aria-labelledby={`question-${pendingKey}-${String(index)}`}
        aria-busy={busy !== null}
      >
        <header className="question-header">
          <div className="question-heading">
            {question.header !== undefined && <div className="question-eyebrow">{question.header}</div>}
            <h2 className="question-title" id={`question-${pendingKey}-${String(index)}`}>
              {question.question}
            </h2>
          </div>
          <div className="question-header-actions">
            {waitLabel !== undefined && <span className="question-wait-status">{waitLabel}</span>}
            {showTakeTime && (
              <button type="button" className="btn" onClick={onTakeTime}>
                {COPY.takeTime}
              </button>
            )}
            <button
              type="button"
              className="btn ghost small"
              aria-label={minimized ? COPY.maximize : COPY.minimize}
              title={minimized ? COPY.maximize : COPY.minimize}
              aria-expanded={!minimized}
              disabled={busy !== null}
              onClick={() => { setMinimized((current) => !current); }}
            >
              {minimized ? "▴" : "▾"}
            </button>
            <button
              type="button"
              className="btn ghost small"
              aria-label={dismissLabel}
              title={dismissLabel}
              disabled={busy !== null}
              onClick={dismissFlow}
            >
              ×
            </button>
          </div>
        </header>

        {!minimized && (
          <>
            <div
              className="question-body"
              data-question-scroll
              onFocusCapture={() => { onHoldFocus?.(); }}
              onBlurCapture={() => { onReleaseFocus?.(); }}
            >
              {question.detail !== undefined && (
                <div className="question-detail">
                  <MarkdownText text={question.detail} variant="compact" />
                </div>
              )}
              <div className="question-options" role={question.multiSelect === true ? "group" : "radiogroup"}>
                {(question.options ?? []).map((option, optionIndex) => {
                  const selected = draft.selected.includes(option.label);
                  const display = parseRecommendedLabel(option.label);
                  return (
                    <button
                      type="button"
                      key={`${option.label}-${String(optionIndex)}`}
                      className={`question-option${selected && question.multiSelect !== true ? " selected" : ""}`}
                      role={question.multiSelect === true ? "checkbox" : "radio"}
                      aria-checked={selected}
                      aria-label={display.label}
                      disabled={locked}
                      onClick={() => { choose(option.label); }}
                      onKeyDown={(event) => {
                        if (event.key !== "Enter") return;
                        event.preventDefault();
                        submitDrafts(drafts);
                      }}
                    >
                      {question.multiSelect === true
                        ? <span className={`question-check${selected ? " checked" : ""}`} aria-hidden="true">{selected ? "✓" : ""}</span>
                        : <span className="question-number">{optionIndex + 1}</span>}
                      <span className="question-option-copy">
                        <span className="question-option-label">{display.label}</span>
                        {display.recommended && <span className="question-badge">{COPY.recommended}</span>}
                        {option.description !== undefined && (
                          <span className="question-option-description">{option.description}</span>
                        )}
                      </span>
                    </button>
                  );
                })}

                {hasOptions ? (
                  <div className={`question-custom-row${draft.custom !== "" ? " active" : ""}${question.multiSelect !== true && draft.selected.length > 0 ? " yielded" : ""}`}>
                    {question.multiSelect === true
                      ? <span className={`question-check${draft.custom !== "" ? " checked" : ""}`} aria-hidden="true">{draft.custom !== "" ? "✓" : ""}</span>
                      : <span className="question-number" aria-hidden="true">✎</span>}
                    <AnswerField
                      variant="inline"
                      value={draft.custom}
                      disabled={locked}
                      onChange={draftCustom}
                      onKeyDown={continueFromCustom}
                    />
                  </div>
                ) : (
                  <AnswerField
                    autoFocus={!locked && !focusedQuestions.current.has(index)}
                    variant="block"
                    value={draft.custom}
                    disabled={locked}
                    onFocus={() => { focusedQuestions.current.add(index); }}
                    onChange={draftCustom}
                    onKeyDown={continueFromCustom}
                  />
                )}
              </div>
            </div>

            <footer className="question-footer">
              <div className="question-pager">
                <button
                  type="button"
                  className="btn ghost small"
                  aria-label={COPY.prev}
                  disabled={index === 0 || busy !== null}
                  onClick={() => { replaceProgress(index - 1, drafts); setError(null); }}
                >
                  ‹
                </button>
                <span className="question-progress">{index + 1} / {questions.length}</span>
                <button
                  type="button"
                  className="btn ghost small"
                  aria-label={COPY.next}
                  disabled={index === questions.length - 1 || busy !== null}
                  onClick={() => { replaceProgress(index + 1, drafts); setError(null); }}
                >
                  ›
                </button>
              </div>
              <div className="question-feedback" role="status">{error}</div>
              <div className="question-footer-actions">
                <button type="button" className="btn" disabled={busy !== null} onClick={skipQuestion}>
                  {COPY.skip}
                </button>
                <button
                  type="button"
                  className="btn primary"
                  disabled={busy !== null || !answeredDraft(draft) || (index === questions.length - 1 && !canSubmit)}
                  onClick={continueFlow}
                >
                  {busy === "answer" ? COPY.submitting : index === questions.length - 1 ? COPY.submit : COPY.actionNext}
                </button>
              </div>
            </footer>
          </>
        )}
      </section>
    </div>
  );
}
