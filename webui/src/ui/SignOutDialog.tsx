// Account sign-out confirmation with the current task impact (upstream
// ui-settings-account/src/client/SignOutDialog.tsx).

import { useState } from "react";

export interface SignOutDialogProps {
  /** Task impact sampled when the menu asked to sign out; 'unknown' when that query failed. */
  running: boolean | "unknown";
  signOut: () => Promise<void>;
  close: () => void;
}

export function SignOutDialog({ running, signOut, close }: SignOutDialogProps) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const dismiss = (): void => {
    if (!busy) close();
  };
  const confirm = async (): Promise<void> => {
    setBusy(true);
    setFailed(false);
    try {
      await signOut();
      close();
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div
      className="dialog-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) dismiss();
      }}
    >
      <div className="dialog" role="dialog" aria-modal="true" aria-label="退出登录">
        <div className="dialog-head">
          <h2 className="dialog-title">退出登录</h2>
          <button
            type="button"
            className="dialog-close"
            aria-label="关闭"
            disabled={busy}
            onClick={dismiss}
          >
            ×
          </button>
        </div>
        <p className="dialog-description">
          {running === "unknown"
            ? "暂时无法确认任务状态。退出登录可能会中断使用此账号的任务，是否继续？"
            : running
              ? "当前有进程正在执行中，点击“退出登录”，将会打断进程，是否即刻退出登录？"
              : "退出登录不会丢失任何数据，你仍可以登录此账号。"}
        </p>
        {failed && (
          <p className="dialog-description dialog-error" role="alert">
            操作未完成，请重试。
          </p>
        )}
        <div className="dialog-actions">
          <button type="button" className="btn small ghost" disabled={busy} onClick={dismiss}>
            取消
          </button>
          <button
            type="button"
            className="btn small danger"
            disabled={busy}
            onClick={() => void confirm()}
          >
            退出登录
          </button>
        </div>
      </div>
    </div>
  );
}
