import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import type {
  CommandDescriptor,
  ComposerServices,
  PromptMode,
  PromptPart,
  SkillDescriptor,
} from "../wire/composer";
import type { PermissionCatalog, PermissionSelection } from "../wire";
import type { PlanSelection } from "../wire/plan";
import {
  detectPermissionArguments,
  detectTrigger,
  fileCandidates,
  formatFileMention,
  formatSessionMention,
  parseSlashLine,
  permissionCandidates,
  replaceRange,
  sessionCandidates,
  slashCandidates,
} from "./triggers";
import type { MenuCandidate } from "./triggers";
import { usePreferences } from "../app/usePreferences";
import { requiresRiskConfirmation, riskCopyFor } from "../app/permissionPresets";
import { PermissionSelect } from "./PermissionSelect";
import { PlanChip } from "./PlanChip";
import { RiskConfirmation } from "../ui/RiskConfirmation";

interface Props {
  sessionId: string | null;
  running: boolean;
  services: ComposerServices;
  /** Session `permissions` projection value; null when the capability is absent. */
  permission?: PermissionSelection | null;
  permissionCatalog?: PermissionCatalog | null;
  /** Folded `plan` unit; null when neither the projection nor the log is available. */
  plan?: PlanSelection | null;
  /** Turn plan mode off (`/plan off`). Return a failure string to show on the chip. */
  onExitPlan?: () => Promise<string | null>;
}

interface Selection {
  start: number;
  end: number;
}

interface AttachmentChip {
  key: string;
  name: string;
  receiptId: string | null;
}

interface Catalog {
  commands: CommandDescriptor[];
  skills: SkillDescriptor[];
}

const EMPTY_CATALOG: Catalog = { commands: [], skills: [] };
const DRAFT_PREFIX = "miniharness.draft:";

function draftKey(sessionId: string): string {
  return `${DRAFT_PREFIX}${sessionId}`;
}

function loadDraft(sessionId: string | null): { text: string; selection: Selection } {
  if (sessionId === null) return { text: "", selection: { start: 0, end: 0 } };
  const raw = localStorage.getItem(draftKey(sessionId));
  if (raw === null) return { text: "", selection: { start: 0, end: 0 } };
  const parsed = JSON.parse(raw) as { text?: unknown; selection?: Partial<Selection> };
  const text = typeof parsed.text === "string" ? parsed.text : "";
  const start = typeof parsed.selection?.start === "number" ? parsed.selection.start : text.length;
  const end = typeof parsed.selection?.end === "number" ? parsed.selection.end : start;
  return { text, selection: { start: Math.min(start, text.length), end: Math.min(end, text.length) } };
}

function saveDraft(sessionId: string | null, text: string, selection: Selection): void {
  if (sessionId === null) return;
  if (text === "") {
    localStorage.removeItem(draftKey(sessionId));
    return;
  }
  localStorage.setItem(draftKey(sessionId), JSON.stringify({ text, selection }));
}

function scrollCaretIntoView(el: HTMLTextAreaElement): void {
  const schedule = typeof requestAnimationFrame === "function"
    ? requestAnimationFrame
    : (callback: FrameRequestCallback) => setTimeout(() => callback(Date.now()), 0);
  schedule(() => {
    const caret = el.selectionStart;
    if (caret === null || el.value === "") return;
    const lineHeight = Number.parseFloat(getComputedStyle(el).lineHeight) || 21;
    const before = el.value.slice(0, caret);
    const line = before.split("\n").length - 1;
    const top = line * lineHeight;
    const viewport = el.clientHeight || 0;
    if (caret === el.value.length) {
      el.scrollTop = el.scrollHeight;
      return;
    }
    if (top < el.scrollTop) el.scrollTop = top;
    else if (top + lineHeight > el.scrollTop + viewport) {
      el.scrollTop = Math.max(0, top + lineHeight - viewport);
    }
  });
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function Composer({
  sessionId,
  running,
  services,
  permission = null,
  permissionCatalog = null,
  plan = null,
  onExitPlan,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const savedSelection = useRef<Selection>({ start: 0, end: 0 });
  const catalogCache = useRef<{
    sessionId: string;
    promise: Promise<Catalog>;
  } | null>(null);
  const sessionRef = useRef<string | null>(sessionId);
  sessionRef.current = sessionId;

  const [draft, setDraft] = useState("");
  const [caret, setCaret] = useState(0);
  const [commands, setCommands] = useState<CommandDescriptor[]>([]);
  const [skills, setSkills] = useState<SkillDescriptor[]>([]);
  const [candidates, setCandidates] = useState<MenuCandidate[]>([]);
  const [menuIndex, setMenuIndex] = useState(0);
  const [menuLoading, setMenuLoading] = useState(false);
  const [attachments, setAttachments] = useState<AttachmentChip[]>([]);
  const { preferences, update: updatePreferences } = usePreferences();
  const busyMode: PromptMode = preferences.busyEnter === "steer" ? "steer" : "queue";
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [permissionConfirm, setPermissionConfirm] = useState<string | null>(null);
  const [permissionAcknowledged, setPermissionAcknowledged] = useState(false);

  const hit = useMemo(() => detectTrigger(draft, caret), [draft, caret]);
  const permissionHit = useMemo(
    () =>
      sessionId === null || permissionCatalog === null
        ? null
        : detectPermissionArguments(draft, caret),
    [caret, draft, permissionCatalog, sessionId],
  );

  const loadCatalogs = useCallback(async (): Promise<Catalog> => {
    if (sessionId === null) return EMPTY_CATALOG;
    const cached = catalogCache.current;
    if (cached !== null && cached.sessionId === sessionId) return cached.promise;
    const promise = (async () => {
      try {
        const [nextCommands, nextSkills] = await Promise.all([
          services.listCommands(sessionId),
          services.listSkills(sessionId),
        ]);
        if (sessionRef.current === sessionId) {
          setCommands(nextCommands);
          setSkills(nextSkills);
        }
        return { commands: nextCommands, skills: nextSkills };
      } catch (cause) {
        if (sessionRef.current === sessionId) {
          catalogCache.current = null;
          setError(errorMessage(cause));
        }
        return EMPTY_CATALOG;
      }
    })();
    catalogCache.current = { sessionId, promise };
    return promise;
  }, [services, sessionId]);

  useEffect(() => {
    const restored = loadDraft(sessionId);
    setDraft(restored.text);
    setCaret(restored.selection.end);
    savedSelection.current = restored.selection;
    setCommands([]);
    setSkills([]);
    setCandidates([]);
    setMenuLoading(false);
    setAttachments([]);
    setError(null);
    setPermissionAcknowledged(false);
    setPermissionConfirm(null);
    catalogCache.current = null;
    if (sessionId !== null) void loadCatalogs();
  }, [loadCatalogs, sessionId]);

  useEffect(() => {
    saveDraft(sessionId, draft, savedSelection.current);
  }, [caret, draft, sessionId]);

  useEffect(() => {
    if (hit === null || hit.trigger !== "/" || sessionId === null || permissionHit !== null) return;
    setCandidates(slashCandidates(commands, skills, hit.query));
    setMenuIndex(0);
  }, [commands, hit, permissionHit, sessionId, skills]);

  useEffect(() => {
    if (permissionHit === null) return;
    // A complete preset value is the argued command path: the line already
    // names its target, so Enter must run it as typed instead of accepting a
    // menu row (the risk gate belongs to the pickers, not the typed command).
    if (permissionCatalog?.options.some((option) => option.value === permissionHit.query) === true) {
      setCandidates([]);
      setMenuLoading(false);
      return;
    }
    const next = permissionCandidates(
      permissionCatalog?.options ?? [],
      permission?.currentValue ?? null,
      permissionHit.query,
    );
    setCandidates(next);
    setMenuIndex(Math.max(0, next.findIndex((candidate) => candidate.active === true)));
    setMenuLoading(false);
  }, [permission, permissionCatalog, permissionHit]);

  useEffect(() => {
    if (hit === null || hit.trigger !== "@" || sessionId === null) {
      if (hit?.trigger !== "@") setMenuLoading(false);
      return;
    }
    let cancelled = false;
    setMenuLoading(true);
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const [files, sessions] = await Promise.all([
            services.listFileReferences(sessionId, hit.query),
            services.listSessionReferences(sessionId, hit.query),
          ]);
          if (cancelled) return;
          setCandidates([
            ...fileCandidates(files, hit.query),
            ...sessionCandidates(
              sessions.filter((candidate) => candidate.sessionId !== sessionId),
              hit.query,
            ),
          ]);
          setMenuIndex(0);
          setMenuLoading(false);
        } catch (cause) {
          if (cancelled) return;
          setCandidates([]);
          setMenuLoading(false);
          setError(errorMessage(cause));
        }
      })();
    }, 80);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [hit, services, sessionId]);

  useEffect(() => {
    if (hit?.trigger !== "@" && hit?.trigger !== "/" && permissionHit === null) {
      setCandidates([]);
      setMenuLoading(false);
    }
  }, [hit, permissionHit]);

  useEffect(() => {
    const el = menuRef.current?.querySelector('[aria-selected="true"]');
    if (el !== null && el !== undefined && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ block: "nearest" });
    }
  }, [candidates, menuIndex]);

  const menuOpen = permissionHit !== null
    ? candidates.length > 0
    : hit !== null && (menuLoading || candidates.length > 0);
  const activeIndex = Math.min(menuIndex, Math.max(0, candidates.length - 1));

  const focusComposer = () => {
    const el = textareaRef.current;
    if (el === null) return;
    el.focus();
    const selection = savedSelection.current;
    el.setSelectionRange(
      Math.min(selection.start, el.value.length),
      Math.min(selection.end, el.value.length),
    );
  };

  const updateCaret = (el: HTMLTextAreaElement) => {
    const selection = { start: el.selectionStart, end: el.selectionEnd };
    savedSelection.current = selection;
    setCaret(selection.end);
    saveDraft(sessionId, el.value, selection);
  };

  const applyInsertion = (insertion: string) => {
    if (hit === null) return;
    const next = replaceRange(draft, hit.start, hit.end, insertion);
    setDraft(next.text);
    setCaret(next.caret);
    savedSelection.current = { start: next.caret, end: next.caret };
    const el = textareaRef.current;
    if (el !== null) {
      el.value = next.text;
      el.setSelectionRange(next.caret, next.caret);
      el.focus();
      scrollCaretIntoView(el);
    }
  };

  const pick = (candidate: MenuCandidate) => {
    if (candidate.kind === "permission") {
      if (requiresRiskConfirmation(candidate.value)) {
        setCandidates([]);
        setMenuLoading(false);
        setPermissionAcknowledged(false);
        setPermissionConfirm(candidate.value);
        return;
      }
      void executePermission(candidate.value);
      return;
    }
    if (candidate.kind === "command" || candidate.kind === "skill") {
      applyInsertion(`/${candidate.value} `);
      return;
    }
    if (candidate.kind === "file" || candidate.kind === "directory") {
      const mention = formatFileMention(
        { path: candidate.value, kind: candidate.kind },
        hit?.quoted ?? false,
      );
      if (mention === undefined) {
        setError(`路径无法安全引用：${candidate.value}`);
        return;
      }
      applyInsertion(candidate.kind === "directory" ? mention : `${mention} `);
      return;
    }
    const mention =
      candidate.mention ??
      formatSessionMention({
        sessionId: candidate.value,
        label: candidate.label,
        displayTitle: candidate.label,
      });
    applyInsertion(`${mention} `);
  };

  const clearDraft = () => {
    setDraft("");
    setCaret(0);
    savedSelection.current = { start: 0, end: 0 };
    saveDraft(sessionId, "", { start: 0, end: 0 });
    setAttachments([]);
    setCandidates([]);
    setMenuIndex(0);
    const el = textareaRef.current;
    if (el !== null) {
      el.value = "";
      el.setSelectionRange(0, 0);
    }
  };

  /** Run one `/permission <preset>` switch through the session command path. */
  const executePermission = async (value: string): Promise<void> => {
    if (sessionId === null) return;
    setError(null);
    try {
      const execution = await services.executeCommand(sessionId, `/permission ${value}`, []);
      if (execution === null) return;
      if (execution.result.kind === "error") {
        setError(execution.result.text ?? "命令执行失败");
      }
      clearDraft();
    } catch (cause) {
      setError(errorMessage(cause));
    }
  };

  const permissionRisk =
    permissionConfirm === null ? null : riskCopyFor(permissionConfirm, "session");

  const submit = async () => {
    if (sessionId === null || submitting) return;
    const text = draft.trim();
    if (text === "" && attachments.length === 0) return;
    if (attachments.some((attachment) => attachment.receiptId === null)) return;
    setError(null);
    setSubmitting(true);
    try {
      const catalog = text.startsWith("/") ? await loadCatalogs() : EMPTY_CATALOG;
      const files: PromptPart[] = attachments.map((attachment) => ({
        type: "file",
        receiptId: attachment.receiptId!,
      }));
      const content: PromptPart[] = [
        ...(text === "" ? [] : [{ type: "text" as const, text }]),
        ...files,
      ];
      const mode: PromptMode = running ? busyMode : "queue";
      if (text.startsWith("/")) {
        const parsed = parseSlashLine(text);
        if (parsed !== null) {
          const command = catalog.commands.find((item) => item.name === parsed.name);
          const skill = catalog.skills.find((item) => item.name === parsed.name);
          if (command !== undefined && skill === undefined) {
            const execution = await services.executeCommand(sessionId, text, files);
            if (execution !== null) {
              if (execution.result.kind === "error") {
                setError(execution.result.text ?? "命令执行失败");
              }
              clearDraft();
              return;
            }
          } else if (skill !== undefined && command === undefined) {
            await services.sendPrompt(sessionId, content, mode);
            clearDraft();
            return;
          }
        }
      }
      await services.sendPrompt(sessionId, content, mode);
      clearDraft();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSubmitting(false);
    }
  };

  const upload = async (files: FileList) => {
    if (sessionId === null) return;
    setError(null);
    for (const file of Array.from(files)) {
      const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      setAttachments((current) => [...current, { key, name: file.name, receiptId: null }]);
      try {
        const uploaded = await services.uploadFile(sessionId, file);
        setAttachments((current) =>
          current.map((attachment) =>
            attachment.key === key
              ? { ...attachment, receiptId: uploaded.receiptId }
              : attachment,
          ),
        );
      } catch (cause) {
        setAttachments((current) => current.filter((attachment) => attachment.key !== key));
        setError(errorMessage(cause));
      }
    }
  };

  const disabled = sessionId === null;
  const placeholder = disabled
    ? "先选择或新建会话"
    : running && busyMode === "steer"
      ? "输入插话，Enter 立即发送…"
      : "Message or run a task, / commands, @ files or sessions";

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (menuOpen) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setMenuIndex((current) => (current + 1) % Math.max(1, candidates.length));
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setMenuIndex((current) =>
          (current - 1 + Math.max(1, candidates.length)) % Math.max(1, candidates.length),
        );
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        if (event.key === "Tab" && event.shiftKey) return;
        const candidate = candidates[activeIndex];
        if (candidate !== undefined) {
          event.preventDefault();
          pick(candidate);
          return;
        }
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setCandidates([]);
        setMenuLoading(false);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  };

  return (
    <div
      className="composer"
      onMouseDown={(event) => {
        const target = event.target as Element;
        if (target.closest("button, input, textarea, a, label, .composer-menu") !== null) return;
        event.preventDefault();
        focusComposer();
      }}
    >
      {menuOpen && (
        <div
          className="composer-menu"
          role="listbox"
          aria-label={permissionHit !== null ? "/permission 匹配项" : "Trigger suggestions"}
          ref={menuRef}
        >
          {menuLoading && candidates.length === 0 && (
            <div className="composer-menu-status" role="status">
              加载候选中…
            </div>
          )}
          {candidates.map((candidate, index) => {
            const first = index === 0 || candidates[index - 1].group !== candidate.group;
            const selected = index === activeIndex;
            return (
              <div key={candidate.id}>
                {first && <div className="composer-menu-group">{candidate.group}</div>}
                <button
                  type="button"
                  role="option"
                  aria-selected={selected}
                  aria-current={candidate.active === true ? "true" : undefined}
                  className={`composer-menu-row${selected ? " active" : ""}${candidate.active === true ? " current" : ""}`}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseEnter={() => setMenuIndex(index)}
                  onClick={() => pick(candidate)}
                >
                  <span className="composer-menu-label">{candidate.label}</span>
                  {candidate.badge !== undefined && (
                    <span className="composer-menu-badge">{candidate.badge}</span>
                  )}
                  {candidate.detail !== undefined && (
                    <span className="composer-menu-detail">{candidate.detail}</span>
                  )}
                </button>
              </div>
            );
          })}
        </div>
      )}

      <div className="composer-card">
        {attachments.length > 0 && (
          <div className="composer-attachments">
            {attachments.map((attachment) => (
              <span
                className={`composer-attachment${attachment.receiptId === null ? " pending" : ""}`}
                key={attachment.key}
              >
                <span>{attachment.name}</span>
                {attachment.receiptId === null && <span className="dim">上传中</span>}
                <button
                  type="button"
                  aria-label={`移除 ${attachment.name}`}
                  onClick={() =>
                    setAttachments((current) =>
                      current.filter((item) => item.key !== attachment.key),
                    )
                  }
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}
        {error !== null && (
          <div className="composer-error" role="alert">
            {error}
          </div>
        )}
        <textarea
          ref={textareaRef}
          className="composer-input"
          aria-label="Message or run a task, / commands, @ files or sessions"
          placeholder={placeholder}
          rows={1}
          value={draft}
          disabled={disabled}
          onChange={(event) => {
            const el = event.currentTarget;
            setDraft(el.value);
            updateCaret(el);
            scrollCaretIntoView(el);
          }}
          onSelect={(event) => updateCaret(event.currentTarget)}
          onKeyUp={(event) => updateCaret(event.currentTarget)}
          onMouseUp={(event) => updateCaret(event.currentTarget)}
          onKeyDown={onKeyDown}
          onBlur={() => saveDraft(sessionId, draft, savedSelection.current)}
          onFocus={(event) => {
            const length = event.currentTarget.value.length;
            if (length > 0 && savedSelection.current.start === 0 && savedSelection.current.end === 0) {
              savedSelection.current = { start: length, end: length };
            }
            const selection = savedSelection.current;
            event.currentTarget.setSelectionRange(
              Math.min(selection.start, length),
              Math.min(selection.end, length),
            );
          }}
          style={{ maxHeight: "calc(14 * 1.5em + 20px)", overflowY: "auto" }}
        />
        <div className="composer-toolbar">
          <PermissionSelect
            selection={permission}
            catalog={permissionCatalog}
            locked={disabled}
            onSelect={executePermission}
          />
          {onExitPlan !== undefined && (
            <PlanChip plan={plan} locked={disabled} onExit={onExitPlan} />
          )}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            onChange={(event) => {
              const files = event.currentTarget.files;
              event.currentTarget.value = "";
              if (files !== null) void upload(files);
            }}
          />
          <button
            type="button"
            className="btn small ghost"
            disabled={disabled}
            onClick={() => fileInputRef.current?.click()}
          >
            Add files or run commands
          </button>
          <span className="spacer" />
          {running && (
            <div className="composer-mode" role="group" aria-label="Busy Enter mode">
              <button
                type="button"
                className={`btn small${busyMode === "queue" ? "" : " ghost"}`}
                onClick={() => updatePreferences({ busyEnter: "queue" })}
              >
                排队
              </button>
              <button
                type="button"
                className={`btn small${busyMode === "steer" ? "" : " ghost"}`}
                onClick={() => updatePreferences({ busyEnter: "steer" })}
              >
                插话
              </button>
            </div>
          )}
          <button
            type="button"
            className="btn"
            disabled={
              disabled ||
              submitting ||
              attachments.some((attachment) => attachment.receiptId === null) ||
              (draft.trim() === "" && attachments.length === 0)
            }
            onClick={() => void submit()}
          >
            {running ? (busyMode === "steer" ? "插话" : "排队") : "发送"}
          </button>
        </div>
      </div>

      {permissionConfirm !== null && permissionRisk !== null && (
        <RiskConfirmation
          open
          title={permissionRisk.title}
          description={permissionRisk.description}
          acknowledgeLabel={permissionRisk.acknowledgeLabel}
          cancelLabel={permissionRisk.cancelLabel}
          closeLabel="关闭"
          confirmLabel={permissionRisk.confirmLabel}
          acknowledged={permissionAcknowledged}
          disabled={disabled}
          onAcknowledgedChange={setPermissionAcknowledged}
          onCancel={() => {
            setPermissionAcknowledged(false);
            setPermissionConfirm(null);
            focusComposer();
          }}
          onConfirm={() => {
            const value = permissionConfirm;
            setPermissionAcknowledged(false);
            setPermissionConfirm(null);
            void executePermission(value);
          }}
        />
      )}
    </div>
  );
}
