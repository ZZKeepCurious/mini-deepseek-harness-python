// Frame-wide quota notice (upstream ui-chat QuotaNoticeHost + account
// AccountQuotaNotice): one live notice for the whole app, so leaving the Chat
// panel does not drop it. A signed-in viewer gets the account copy and a
// top-up entry from the Host-projected account links; a signed-out or
// unavailable account falls back to the generic provider message.

import { useEffect } from "react";

export interface QuotaNoticeProps {
  /** Provider code that raised the notice (QUOTA or ACCOUNT_QUOTA). */
  code: string;
  /** Whether the account projection currently names a stored credential. */
  signedIn: boolean;
  /** Host-projected top-up destination; undefined until the account stream answers. */
  topUpUrl: string | undefined;
  onDismiss: () => void;
}

/** How long the generic toast stays before it fades on its own. */
const TOAST_LIFETIME_MS = 8_000;

export function QuotaNotice({ code, signedIn, topUpUrl, onDismiss }: QuotaNoticeProps) {
  useEffect(() => {
    const timer = setTimeout(onDismiss, TOAST_LIFETIME_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [onDismiss]);

  return (
    <div className="quota-notice" role="alert" data-code={code}>
      <span className="quota-notice-icon" aria-hidden="true" />
      <div className="quota-notice-copy">
        {signedIn ? (
          <>
            <span className="quota-notice-title">当前无可用余额</span>
            <span className="quota-notice-message">
              没有可用额度时，DeepSeek Harness 无法开始新的任务，是否前往充值？或者可以稍后前往
              设置 → 账号与余额 进行充值。
            </span>
          </>
        ) : (
          <span className="quota-notice-message">当前请求的额度已用尽</span>
        )}
      </div>
      <div className="quota-notice-actions">
        {signedIn && topUpUrl !== undefined && (
          <a
            className="btn small primary"
            href={topUpUrl}
            target="_blank"
            rel="noreferrer"
            onClick={onDismiss}
          >
            去充值
          </a>
        )}
        <button type="button" className="btn small ghost" onClick={onDismiss}>
          关闭
        </button>
      </div>
    </div>
  );
}
