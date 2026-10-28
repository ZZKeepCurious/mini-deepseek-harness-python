// Account settings section (upstream
// ui-settings-account/src/client/AccountSection.tsx): safe Host state and
// explicit login actions. The balance rows share the Platform entry with the
// usage action, and a failed wallet read still reaches the same destination so
// the user can inspect the balance this build could not load.

import { Big } from "big.js";
import { useEffect, useState } from "react";
import { ACCOUNT_ACTIVE_PHASES } from "../wire";
import type { UseAccount } from "../app/useAccount";
import { authorizeUrlWithTheme } from "../app/authorizeUrl";
import { balanceSymbol, formatBalance } from "../app/formatBalance";
import { AccountAvatar } from "./AccountAvatar";

export interface AccountSectionProps {
  account: UseAccount;
  /** Resolved scheme of the active application theme; the authorization link carries it. */
  colorScheme: "light" | "dark";
}

export function AccountSection({ account, colorScheme }: AccountSectionProps) {
  const { view: state, details, avatar, failed: streamFailed, avatarFailed } = account;
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const profile = details?.profile?.status === "ready" ? details.profile.value : undefined;
  const wallets = details?.balance?.status === "ready" ? details.balance.value : undefined;
  const bonusWallets =
    details?.balance?.status === "ready"
      ? details.balance.bonusWallets.filter((wallet) => new Big(wallet.balance).gt(0))
      : [];
  const showBonusRow = details?.balance?.status !== "ready" || bonusWallets.length > 0;
  const attempt = state?.attempt;
  const active =
    attempt !== null &&
    attempt !== undefined &&
    ACCOUNT_ACTIVE_PHASES.includes(attempt.phase);
  const signedIn = state?.status === "credential-stored";
  useEffect(() => {
    if (signedIn) return;
    setBusy(false);
    setFailed(false);
  }, [signedIn]);
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
  /** Web builds keep the Platform destination in a new tab. */
  const platformLink = (label: string, className: string | undefined) => (
    <a
      className={className}
      href={state?.links.usageUrl}
      aria-disabled={state === undefined}
      target="_blank"
      rel="noreferrer"
    >
      {label}
    </a>
  );
  const status =
    failed || streamFailed || attempt?.phase === "failed"
      ? "操作未完成，请重试。"
      : attempt?.phase === "expired"
        ? "登录已过期，请重试。"
        : active && attempt !== null && attempt !== undefined
          ? attempt.phase === "initializing"
            ? "正在发起登录…"
            : attempt.phase === "waiting-browser"
              ? "请在浏览器中继续"
              : "正在完成登录…"
          : signedIn
            ? (profile?.contact ??
              (details?.profile === undefined ? "加载中…" : "账号资料暂不可用"))
            : "登录 DeepSeek 账号以开始使用";
  if (!signedIn && !active) {
    return (
      <section className="account-section-signed-out" aria-label="账号与余额">
        <div className="account-signed-out-copy">
          <span className="account-signed-out-title">
            当前未登录 DeepSeek Harness 账号
          </span>
          <span className="account-signed-out-description" role="status">
            {failed || streamFailed
              ? "操作未完成，请重试。"
              : "登录 DeepSeek Harness 账号获取专属 API Key"}
          </span>
        </div>
        <button
          type="button"
          className="btn small"
          disabled={busy || state === undefined}
          onClick={() => void run(account.start)}
        >
          登录
        </button>
      </section>
    );
  }
  return (
    <section className="account-section" aria-label="账号与余额">
      <div className="account-card">
        <div className="account-identity">
          <span
            className="account-avatar"
            data-avatar-status={signedIn ? avatar?.status : undefined}
          >
            <AccountAvatar
              avatar={signedIn ? avatar : undefined}
              avatarFailed={avatarFailed}
            />
          </span>
          <div className="account-identity-copy">
            <span className="account-name">
              {signedIn ? (profile?.name ?? "已登录 DeepSeek") : "尚未登录"}
            </span>
            <span className="account-status" role="status">
              {status}
            </span>
          </div>
        </div>
        {signedIn && state !== null && (
          <a
            className="account-info"
            href={new URL("/", state.links.usageUrl).href}
            target="_blank"
            rel="noopener noreferrer"
          >
            更多账号信息
          </a>
        )}
      </div>
      {active && attempt !== null && attempt !== undefined && (
        <div className="account-actions">
          {attempt.authorizeUrl && (
            <a
              className="btn small ghost"
              href={authorizeUrlWithTheme(attempt.authorizeUrl, colorScheme)}
              target="_blank"
              rel="noreferrer"
            >
              打开浏览器
            </a>
          )}
          <button
            type="button"
            className="btn small ghost"
            disabled={busy || attempt.phase === "committing"}
            onClick={() => void run(() => account.cancel(attempt.id))}
          >
            取消
          </button>
        </div>
      )}
      <div className="account-balance-card">
        <div className="account-row">
          <span>充值余额</span>
          {signedIn && wallets !== undefined && wallets.length > 0 ? (
            <span className="account-amount">
              {wallets.map((wallet) => (
                <span key={wallet.currency}>
                  {formatBalance(wallet.balance, balanceSymbol(wallet.currency))}
                </span>
              ))}
            </span>
          ) : !signedIn || details?.balance === undefined ? (
            <span className="account-unavailable">
              {!signedIn ? "登录后查看" : "加载中…"}
            </span>
          ) : (
            platformLink("前往开放平台查看", "account-unavailable-link")
          )}
        </div>
        {signedIn && showBonusRow && (
          <>
            <div className="account-divider" />
            <div className="account-row">
              <span>赠金余额</span>
              <span className="account-bonus-value">
                {bonusWallets.length > 0 ? (
                  <span className="account-amount">
                    {bonusWallets.map((wallet) => (
                      <span key={wallet.currency}>
                        {formatBalance(wallet.balance, balanceSymbol(wallet.currency))}
                      </span>
                    ))}
                  </span>
                ) : details?.balance === undefined ? (
                  <span className="account-unavailable">加载中…</span>
                ) : (
                  platformLink("前往开放平台查看", "account-unavailable-link")
                )}
              </span>
            </div>
          </>
        )}
        <div className="account-divider" />
        <div className="account-row">
          <span className="account-secondary">更多</span>
          <div className="account-links">
            {platformLink("查询用量", "account-link")}
            <a
              className="account-link account-link-primary"
              href={state?.links.topUpUrl}
              aria-disabled={state === undefined}
              target="_blank"
              rel="noreferrer"
            >
              充值
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}
