// Current-session permission seat: shows the session's `permissions`
// projection, lists the process catalog, and switches the preset by running the
// host `/permission <preset>` command. Source: upstream
// packages/client/ui-permission-presets/src/client/PermissionSelect.tsx — the
// Full access and Auto review rows pass the shared risk acknowledgement first.

import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import type { PermissionCatalog, PermissionSelection } from "../wire";
import {
  displayPermissionPreset,
  presetBadge,
  presetDescription,
  requiresRiskConfirmation,
  riskCopyFor,
} from "../app/permissionPresets";
import { RiskConfirmation } from "../ui/RiskConfirmation";

export interface PermissionSelectProps {
  /** Session projection value; null means the host exposes no permission capability. */
  selection: PermissionSelection | null;
  catalog: PermissionCatalog | null;
  /** Whether the composer currently refuses interaction. */
  locked: boolean;
  /** Run one current-session switch; failures are reported by the caller. */
  onSelect: (preset: string) => Promise<void>;
}

const OPTION_SELECTOR = '[role="menuitem"]';

export function PermissionSelect({ selection, catalog, locked, onSelect }: PermissionSelectProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  const [pick, setPick] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);

  useEffect(() => {
    const usable =
      !locked &&
      selection !== null &&
      catalog !== null &&
      (confirmation === null ||
        catalog.options.some((option) => option.value === confirmation));
    if (usable) return;
    setOpen(false);
    setPick(null);
    setAcknowledged(false);
    setConfirmation(null);
  }, [catalog, confirmation, locked, selection]);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent): void => {
      const root = rootRef.current;
      if (root !== null && event.target instanceof Node && root.contains(event.target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  if (selection === null || catalog === null) return null;

  const options = catalog.options;
  const currentValue =
    pick !== null && options.some((option) => option.value === pick) ? pick : selection.currentValue;
  const current = options.find((option) => option.value === currentValue);
  const currentLabel =
    current === undefined
      ? displayPermissionPreset(currentValue, currentValue)
      : displayPermissionPreset(current.value, current.name);
  const busy = pick !== null || confirmation !== null;

  const submit = (value: string): void => {
    setPick(value);
    void (async () => {
      try {
        await onSelect(value);
      } catch {
        // The composer owns error reporting; the picker only clears its pending mark.
      } finally {
        setPick(null);
      }
    })();
  };

  const choose = (value: string): void => {
    setOpen(false);
    if (value === selection.currentValue) return;
    if (requiresRiskConfirmation(value)) {
      setAcknowledged(false);
      setConfirmation(value);
      return;
    }
    submit(value);
  };

  const closeConfirmation = (): void => {
    setAcknowledged(false);
    setConfirmation(null);
    triggerRef.current?.focus();
  };

  const risk = confirmation === null ? null : riskCopyFor(confirmation, "session");

  const onMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Home" && event.key !== "End") {
      return;
    }
    const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>(OPTION_SELECTOR));
    if (items.length === 0) return;
    event.preventDefault();
    const index = items.findIndex((item) => item === document.activeElement);
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? items.length - 1
          : event.key === "ArrowDown"
            ? (index + 1) % items.length
            : (index - 1 + items.length) % items.length;
    items[next].focus();
  };

  return (
    <div className="permission-select" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="btn small ghost permission-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`访问模式，当前：${currentLabel}`}
        title={current === undefined ? undefined : presetDescription(current)}
        disabled={locked || busy || options.length === 0}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="permission-label">{currentLabel}</span>
        <span className="permission-chevron" aria-hidden>
          ▾
        </span>
      </button>
      {open && (
        <div
          className="permission-menu"
          role="menu"
          aria-label="权限模式"
          onKeyDown={onMenuKeyDown}
        >
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              role="menuitem"
              aria-current={option.value === currentValue ? "true" : undefined}
              title={presetDescription(option)}
              className={`permission-option${option.value === currentValue ? " active" : ""}`}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => choose(option.value)}
            >
              <span className="permission-label">
                {displayPermissionPreset(option.value, option.name)}
              </span>
              {presetBadge(option.value) !== undefined && (
                <sup className="permission-badge">{presetBadge(option.value)}</sup>
              )}
            </button>
          ))}
        </div>
      )}
      {confirmation !== null && risk !== null && (
        <RiskConfirmation
          open
          title={risk.title}
          description={risk.description}
          acknowledgeLabel={risk.acknowledgeLabel}
          cancelLabel={risk.cancelLabel}
          closeLabel="关闭"
          confirmLabel={risk.confirmLabel}
          acknowledged={acknowledged}
          disabled={locked}
          onAcknowledgedChange={setAcknowledged}
          onCancel={closeConfirmation}
          onConfirm={() => {
            const value = confirmation;
            closeConfirmation();
            submit(value);
          }}
        />
      )}
    </div>
  );
}
