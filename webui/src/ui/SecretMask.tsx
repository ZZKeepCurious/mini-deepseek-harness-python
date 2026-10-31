interface Props {
  label: string;
  configured: boolean;
  writable: boolean;
  onSet?: (value: string) => void;
  onClear?: () => void;
}

export function SecretMask({ label, configured, writable, onSet, onClear }: Props) {
  return (
    <div className="secret-mask-row">
      <span className="secret-mask-label">{label}</span>
      <span className="secret-mask-value">{configured ? "已设置（不可读）" : "未设置"}</span>
      {writable && onSet !== undefined && (
        <button
          type="button"
          className="btn small"
          onClick={() => {
            const value = window.prompt(`输入 ${label}`);
            if (value !== null && value.trim() !== "") onSet(value.trim());
          }}
        >
          {configured ? "替换" : "设置"}
        </button>
      )}
      {writable && configured && onClear !== undefined && (
        <button type="button" className="btn small ghost" onClick={onClear}>
          清除
        </button>
      )}
    </div>
  );
}
