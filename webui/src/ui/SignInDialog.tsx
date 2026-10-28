// Account authorization dialog (upstream
// ui-settings-account/src/client/SignInDialog.tsx). Errors allow a retry after
// cancelling any active attempt. The API-key entry is this build's settings
// namespace for the DeepSeek credential, because the web profile has no
// onboarding surface.

import { useEffect, useState } from "react";
import type { AccountView } from "../wire";
import { authorizeUrlWithTheme } from "../app/authorizeUrl";

export interface SignInDialogProps {
  /** Latest Host projection; null until the stream answers. */
  view: AccountView | null;
  /** Whether the stream itself failed. */
  streamFailed: boolean;
  /** Latest explicit start request failed before a Host state was available. */
  loginFailed: boolean;
  /** Resolved scheme of the active application theme; the copied link carries it. */
  colorScheme: "light" | "dark";
  start: () => Promise<void>;
  cancel: (id: string) => Promise<void>;
  close: () => void;
  /** Leave the dialog for this build's API-key configuration. */
  useApiKey: () => void;
}

export function SignInDialog({
  view,
  streamFailed,
  loginFailed,
  colorScheme,
  start,
  cancel,
  close,
  useApiKey,
}: SignInDialogProps) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [copyResult, setCopyResult] = useState<"copiedLink" | "copyFailed" | null>(null);
  const attempt = view?.attempt;
  useEffect(() => {
    setCopyResult(null);
  }, [attempt?.id, attempt?.authorizeUrl]);
  useEffect(() => {
    if (copyResult === null) return;
    const timer = setTimeout(() => {
      setCopyResult(null);
    }, 2000);
    return () => {
      clearTimeout(timer);
    };
  }, [copyResult]);
  const authorizeUrl = attempt?.authorizeUrl;
  const phase = attempt?.phase;
  const active =
    busy ||
    phase === "initializing" ||
    phase === "waiting-browser" ||
    phase === "exchanging" ||
    phase === "committing";
  const expired = phase === "expired";
  const localFailure = failed || loginFailed || streamFailed;
  const error = localFailure || phase === "failed";
  const failureKey =
    !localFailure && phase === "failed" && attempt?.errorCode === "no-response"
      ? "noResponse"
      : "failed";
  const waiting = active && !error;
  const committing = phase === "committing";
  useEffect(() => {
    if (view?.status === "credential-stored") close();
  }, [view?.status, close]);
  const run = async (action: () => Promise<void>): Promise<void> => {
    setBusy(true);
    setFailed(false);
    try {
      await action();
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };
  const dismiss = (): void => {
    if (committing || busy) return;
    if (active && attempt) {
      void run(async () => {
        await cancel(attempt.id);
        close();
      });
    } else {
      close();
    }
  };
  const retry = async (): Promise<void> => {
    if (active && attempt) await cancel(attempt.id);
    await start();
  };
  const copyLink = async (target: string): Promise<void> => {
    try {
      await navigator.clipboard.writeText(authorizeUrlWithTheme(target, colorScheme));
      setCopyResult("copiedLink");
    } catch {
      setCopyResult("copyFailed");
    }
  };
  const title = error
    ? "登录失败"
    : active
      ? "等待登录"
      : expired
        ? "登录已超时"
        : "开始使用";
  return (
    <div
      className="dialog-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) dismiss();
      }}
    >
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title}>
        <div className="dialog-head">
          <h2 className="dialog-title">{title}</h2>
          <button type="button" className="dialog-close" aria-label="关闭" onClick={dismiss}>
            ×
          </button>
        </div>
        {waiting ? (
          <p className="dialog-description">
            没有自动打开新页面？
            <button
              type="button"
              className="dialog-link"
              disabled={!authorizeUrl}
              onClick={authorizeUrl ? () => void copyLink(authorizeUrl) : undefined}
            >
              {copyResult === "copiedLink"
                ? "链接已复制"
                : copyResult === "copyFailed"
                  ? "复制失败"
                  : "复制登录链接"}
            </button>
            ，手动打开登录页完成登录。
          </p>
        ) : (
          <p className="dialog-description">
            {error
              ? failureKey === "noResponse"
                ? "操作未完成，可能是网络异常，请检查你的网络状况后重试"
                : "操作未完成，请重试。"
              : expired
                ? "请重新登录后继续操作。"
                : "登录 DeepSeek 账号，或添加 API Key，即可开始使用。你的项目和文件保存在本地。"}
          </p>
        )}
        <div className="dialog-actions">
          <button
            type="button"
            className="btn small ghost"
            disabled={committing || busy}
            onClick={active ? dismiss : useApiKey}
          >
            {active ? "取消" : "添加 API Key"}
          </button>
          <button
            type="button"
            className="btn small"
            disabled={busy || committing || waiting || view === null}
            aria-label={waiting ? "等待登录" : undefined}
            onClick={() => void run(retry)}
          >
            {waiting ? <span className="dialog-spinner" aria-hidden="true" /> : null}
            {expired || error ? "重新登录" : "登录"}
          </button>
        </div>
      </div>
    </div>
  );
}
