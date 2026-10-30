// Controlled risk acknowledgement dialog shared by the permission pickers and
// the settings row. Source: upstream
// packages/client/ui-primitives/src/RiskConfirmation.tsx — the primary action
// stays unavailable until the caller-controlled acknowledgement is checked.

export interface RiskConfirmationProps {
  open: boolean;
  title: string;
  description: string;
  acknowledgeLabel: string;
  cancelLabel: string;
  closeLabel: string;
  confirmLabel: string;
  acknowledged: boolean;
  disabled?: boolean;
  onAcknowledgedChange: (acknowledged: boolean) => void;
  onCancel: () => void;
  onConfirm: () => void;
}

export function RiskConfirmation({
  open,
  title,
  description,
  acknowledgeLabel,
  cancelLabel,
  closeLabel,
  confirmLabel,
  acknowledged,
  disabled = false,
  onAcknowledgedChange,
  onCancel,
  onConfirm,
}: RiskConfirmationProps) {
  if (!open) return null;
  return (
    <div
      className="dialog-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !disabled) onCancel();
      }}
    >
      <div className="dialog risk-confirmation" role="dialog" aria-modal="true" aria-label={title}>
        <div className="dialog-head">
          <h2 className="dialog-title">{title}</h2>
          <button
            type="button"
            className="dialog-close"
            aria-label={closeLabel}
            disabled={disabled}
            onClick={onCancel}
          >
            ×
          </button>
        </div>
        <p className="dialog-description">{description}</p>
        <label className="risk-ack">
          <input
            type="checkbox"
            checked={acknowledged}
            disabled={disabled}
            autoFocus
            onChange={(event) => onAcknowledgedChange(event.currentTarget.checked)}
          />
          <span>{acknowledgeLabel}</span>
        </label>
        <div className="dialog-actions">
          <button type="button" className="btn small ghost" disabled={disabled} onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className="btn small danger"
            disabled={disabled || !acknowledged}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
