// Status-bar account launcher (upstream
// ui-settings-account/src/client/AccountMenu.tsx): settings navigation and the
// locally authoritative sign-in/sign-out actions. The launcher outlives the
// settings panel, so a false-to-true edge is one Settings entry and re-renders
// inside one open dialog do not read the account again.

import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { createPortal } from "react-dom";
import type { UseAccount } from "../app/useAccount";
import { AccountAvatar } from "./AccountAvatar";
import { BonusNoticeStack } from "./BonusNotice";
import { SignInDialog } from "./SignInDialog";
import { SignOutDialog } from "./SignOutDialog";

/** Full-opacity hold of one status toast; the fade duration follows it in CSS. */
const TOAST_HOLD_MS = 3_000;
/** Hold plus the stylesheet fade. */
const TOAST_LIFETIME_MS = 4_000;

export interface AccountMenuProps {
  account: UseAccount;
  /** Resolved scheme of the active application theme; authorization links carry it. */
  colorScheme: "light" | "dark";
  /** Whether the settings dialog is open, for the entry refresh edge. */
  settingsOpen: boolean;
  onOpenSettings: () => void;
  /** Leave the account dialog for this build's API-key configuration. */
  onAddApiKey: () => void;
}

/** Transient status banner rendered above the status bar. */
function AccountToast({ text, onDone }: { text: string; onDone: () => void }) {
  const latestOnDone = useRef(onDone);
  latestOnDone.current = onDone;
  useEffect(() => {
    const timer = setTimeout(() => {
      latestOnDone.current();
    }, TOAST_LIFETIME_MS);
    return () => {
      clearTimeout(timer);
    };
  }, []);
  return createPortal(
    <div
      className="account-toast"
      role="alert"
      style={{ "--toast-hold": `${String(TOAST_HOLD_MS)}ms` } as CSSProperties}
    >
      {text}
    </div>,
    document.body,
  );
}

export function AccountMenu({
  account,
  colorScheme,
  settingsOpen,
  onOpenSettings,
  onAddApiKey,
}: AccountMenuProps) {
  const anchor = useRef<HTMLDivElement>(null);
  const settingsWasOpen = useRef(false);
  useEffect(() => {
    if (settingsOpen && !settingsWasOpen.current) void account.refresh();
    settingsWasOpen.current = settingsOpen;
  }, [account.refresh, settingsOpen]);
  const signedIn = account.view?.status === "credential-stored";
  const profile = account.details?.profile;
  const label =
    profile === undefined
      ? null
      : profile.status === "ready"
        ? (profile.value.name ?? profile.value.contact ?? "已登录 DeepSeek")
        : "已登录 DeepSeek";
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [signOutImpact, setSignOutImpact] = useState<boolean | "unknown">();
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent): void => {
      if (event.target instanceof Node && menuRef.current?.contains(event.target)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);
  const requestSignOut = async (): Promise<void> => {
    setBusy(true);
    try {
      setSignOutImpact(await account.hasRunningAccountTasks());
      setOpen(false);
    } catch {
      setSignOutImpact("unknown");
      setOpen(false);
    } finally {
      setBusy(false);
    }
  };
  // The account hook publishes `loginFailed` before start rejects, so the dialog owns the report.
  const beginSignIn = (): void => {
    setOpen(false);
    void account.start().catch(() => undefined);
  };
  return (
    <div ref={anchor} className="statusbar-account">
      {account.modelSignInRequired > 0 && (
        <AccountToast
          key={account.modelSignInRequired}
          text="当前模型暂不可用，请登录后再试"
          onDone={account.clearModelSignInRequired}
        />
      )}
      {account.sessionExpired > 0 && (
        <AccountToast
          text="登录信息已失效，请重新登录"
          onDone={account.clearSessionExpired}
        />
      )}
      {signedIn && account.notices.length > 0 && (
        <BonusNoticeStack
          notices={account.notices}
          anchor={anchor}
          title="赠金已到账"
          closeLabel="关闭"
          onShown={account.bonusShown}
          onDismiss={account.bonusDismissed}
        />
      )}
      <div className="account-menu" ref={menuRef}>
        <button
          type="button"
          className="account-trigger"
          data-signed-out={!signedIn}
          aria-label="账号菜单"
          aria-haspopup="menu"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          {signedIn ? (
            <span className="account-trigger-avatar" data-avatar-status={account.avatar?.status}>
              <AccountAvatar avatar={account.avatar} avatarFailed={account.avatarFailed} />
            </span>
          ) : (
            <span className="account-trigger-more" aria-hidden="true">
              ⋯
            </span>
          )}
          <span className="account-trigger-label">{signedIn ? label : "更多"}</span>
        </button>
        {open && (
          <div className="account-menu-popover" role="menu">
            <button
              type="button"
              role="menuitem"
              className="account-menu-item"
              onClick={() => {
                setOpen(false);
                onOpenSettings();
              }}
            >
              设置
            </button>
            {signedIn ? (
              <button
                type="button"
                role="menuitem"
                className="account-menu-item"
                disabled={busy}
                onClick={() => void requestSignOut()}
              >
                退出登录
              </button>
            ) : (
              <button
                type="button"
                role="menuitem"
                className="account-menu-item"
                onClick={beginSignIn}
              >
                登录
              </button>
            )}
          </div>
        )}
      </div>
      {account.loginVisible && (
        <SignInDialog
          view={account.view}
          streamFailed={account.failed}
          loginFailed={account.loginFailed}
          colorScheme={colorScheme}
          start={account.start}
          cancel={account.cancel}
          close={() => account.showLogin(false)}
          useApiKey={() => {
            account.showLogin(false);
            onAddApiKey();
          }}
        />
      )}
      {signedIn && signOutImpact !== undefined && (
        <SignOutDialog
          running={signOutImpact}
          signOut={account.signOut}
          close={() => setSignOutImpact(undefined)}
        />
      )}
    </div>
  );
}
