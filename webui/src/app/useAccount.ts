// Account state for the whole frame (upstream ui-settings-account plugin):
// the `account/watch` projection, the profile/balance reads it triggers, the
// bonus notice lifecycle, and the explicit sign-in/sign-out operations.
//
// The stream carries only the safe projection; every credential stays on the
// Host. A sign-in attempt preopens one browser tab owned by this UI and follows
// the attempt id the projection names.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { EmitSubscribe, UseBackend } from "./useBackend";
import {
  ACCOUNT_ACTIVE_PHASES,
  ACCOUNT_WATCH_ENDPOINT,
  ackBonusNotified,
  accountClientMetadata,
  cancelSignIn,
  getAccountBalance,
  getAccountProfile,
  getUnnotifiedBonuses,
  hasRunningAccountTasks as hasRunningAccountTasksRpc,
  initializeDefaultModel,
  signOut as signOutRpc,
  startSignIn,
  type AccountDetails,
  type AccountProfileOutcome,
  type AccountView,
} from "../wire";
import { AccountAvatarLoader, type AccountAvatarSnapshot } from "./accountAvatar";
import { authorizeUrlWithTheme } from "./authorizeUrl";
import { createBonusNoticeController, type BonusNotice } from "./bonusNotices";
import { BrowserLogin } from "./browserLogin";

export interface UseAccountOptions {
  openStream: UseBackend["openStream"];
  subscribe: EmitSubscribe;
  /** Client version reported to the Platform through AccountClientMetadata. */
  version: string;
  /** Resolved palette of the active theme, carried by authorization links. */
  colorScheme: "light" | "dark";
}

export interface UseAccount {
  /** Latest Host projection; null until the stream answers. */
  view: AccountView | null;
  /** Sanitized profile and balance outcomes; null while loading a frame. */
  details: Partial<AccountDetails> | null;
  /** Shared picture load; absent until a profile supplies a picture or after sign-out. */
  avatar: AccountAvatarSnapshot | undefined;
  /** Whether the state stream failed. */
  failed: boolean;
  /** Server-authored bonus notices in front-to-back order. */
  notices: BonusNotice[];
  /** Explicitly opened account dialog. */
  loginVisible: boolean;
  /** Latest explicit start request failed before a Host state was available. */
  loginFailed: boolean;
  /** Live credential-expiry notification counter (0 while none arrived). */
  sessionExpired: number;
  /** Live model sign-in guidance counter (0 while none arrived). */
  modelSignInRequired: number;
  showLogin: (visible: boolean) => void;
  start: () => Promise<void>;
  cancel: (id: string) => Promise<void>;
  signOut: () => Promise<void>;
  hasRunningAccountTasks: () => Promise<boolean>;
  /** Read profile, balance and unnotified bonus once (Settings entry, top-up return). */
  refresh: () => Promise<void>;
  /** @param orderId - notice whose card passed a presented frame while visible. */
  bonusShown: (orderId: string) => void;
  /** @param orderId - notice the user closed. */
  bonusDismissed: (orderId: string) => void;
  /** @param snapshot - picture attempt whose rendered image failed; superseded attempts are ignored. */
  avatarFailed: (snapshot: AccountAvatarSnapshot) => void;
  clearSessionExpired: () => void;
  clearModelSignInRequired: () => void;
}

export function useAccount({
  openStream,
  subscribe,
  version,
  colorScheme,
}: UseAccountOptions): UseAccount {
  const [view, setView] = useState<AccountView | null>(null);
  const [details, setDetails] = useState<Partial<AccountDetails> | null>(null);
  const [avatar, setAvatar] = useState<AccountAvatarSnapshot | undefined>(undefined);
  const [failed, setFailed] = useState(false);
  const [notices, setNotices] = useState<BonusNotice[]>([]);
  const [loginVisible, setLoginVisible] = useState(false);
  const [loginFailed, setLoginFailed] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(0);
  const [modelSignInRequired, setModelSignInRequired] = useState(0);

  const viewRef = useRef<AccountView | null>(null);
  /** Bumped by every streamed frame, so an older read cannot publish into a newer frame. */
  const revision = useRef(0);
  const refreshing = useRef<Promise<void> | undefined>(undefined);

  const browser = useMemo(
    () =>
      new BrowserLogin((authorizeUrl) => authorizeUrlWithTheme(authorizeUrl, colorScheme)),
    [colorScheme],
  );

  const avatarLoader = useMemo(() => new AccountAvatarLoader(setAvatar), []);

  useEffect(() => () => {
    avatarLoader.dispose();
  }, [avatarLoader]);

  const controller = useMemo(
    () =>
      createBonusNoticeController({
        ackRetryDelayMs: 1_000,
        ackRetryMaxDelayMs: 30_000,
        read: () => getUnnotifiedBonuses(accountClientMetadata(version)),
        acknowledge: (accountId, orderId) =>
          ackBonusNotified(accountId, orderId, accountClientMetadata(version)),
        publish: (next) => setNotices([...next]),
      }),
    [version],
  );

  const refresh = useCallback((): Promise<void> => {
    if (viewRef.current?.status !== "credential-stored") return Promise.resolve();
    if (refreshing.current !== undefined) return refreshing.current;
    const generation = revision.current;
    const publishDetail = async <K extends keyof AccountDetails>(
      field: K,
      query: () => Promise<AccountDetails[K] | null>,
    ): Promise<void> => {
      let value: AccountDetails[K] | null;
      try {
        value = await query();
      } catch {
        value = { status: "failed" } as AccountDetails[K];
      }
      if (generation !== revision.current || value === null) return;
      setDetails((current) => ({ ...(current ?? {}), [field]: value }));
    };
    const request = Promise.all([
      publishDetail("profile", async (): Promise<AccountProfileOutcome | null> => {
        const outcome = await getAccountProfile(accountClientMetadata(version));
        if (generation === revision.current && outcome?.status === "ready") {
          avatarLoader.setUrl(outcome.value.avatarUrl);
        }
        return outcome;
      }),
      publishDetail("balance", () => getAccountBalance(accountClientMetadata(version))),
    ]).then(() => undefined);
    refreshing.current = request;
    void request.finally(() => {
      if (refreshing.current === request) refreshing.current = undefined;
    });
    return request;
  }, [avatarLoader, version]);

  useEffect(() => {
    let disposed = false;
    const stream = openStream(ACCOUNT_WATCH_ENDPOINT, { args: {} });
    void (async () => {
      for (;;) {
        const frame = await stream.next();
        if (frame.type === "item") {
          const next = frame.value as AccountView;
          if (disposed) return;
          revision.current += 1;
          refreshing.current = undefined;
          const initialize =
            next.status === "credential-stored" &&
            next.attempt?.phase === "succeeded" &&
            viewRef.current?.attempt?.phase !== "succeeded";
          viewRef.current = next;
          avatarLoader.reset();
          setView(next);
          setDetails(null);
          setAvatar(undefined);
          setFailed(false);
          browser.update(next);
          if (next.status === "credential-stored") controller.begin();
          else controller.end();
          if (initialize) {
            void initializeDefaultModel().catch(() => {
              console.error("[deepseek-account] default model initialization failed");
            });
          }
          void refresh();
          continue;
        }
        if (disposed) return;
        setFailed(true);
        return;
      }
    })().catch(() => {
      if (!disposed) setFailed(true);
    });
    return () => {
      disposed = true;
      stream.close();
    };
  }, [avatarLoader, browser, controller, openStream, refresh]);

  useEffect(
    () => subscribe("deepseek-account/session-expired", () => setSessionExpired((n) => n + 1)),
    [subscribe],
  );

  useEffect(
    () => subscribe("deepseek-account/model-sign-in-required", () => setModelSignInRequired((n) => n + 1)),
    [subscribe],
  );

  const showLogin = useCallback((visible: boolean) => {
    setLoginVisible(visible);
    setLoginFailed(false);
  }, []);

  const start = useCallback(async () => {
    setLoginVisible(true);
    setLoginFailed(false);
    const request = async (): Promise<AccountView> => {
      const next = await startSignIn(
        accountClientMetadata(version),
        window.location.origin,
        "web",
      );
      viewRef.current = next;
      setView(next);
      return next;
    };
    try {
      await browser.start(request, () => viewRef.current ?? undefined);
    } catch (error) {
      setLoginFailed(true);
      throw error;
    }
  }, [browser, version]);

  const cancel = useCallback(async (id: string) => {
    await cancelSignIn(id);
  }, []);

  const signOut = useCallback(async () => {
    await signOutRpc(accountClientMetadata(version));
  }, [version]);

  const hasRunningAccountTasks = useCallback(async () => hasRunningAccountTasksRpc(), []);

  const refreshAll = useCallback(async () => {
    avatarLoader.retry();
    await Promise.all([refresh(), controller.refresh()]);
  }, [avatarLoader, controller, refresh]);

  const avatarFailed = useCallback(
    (snapshot: AccountAvatarSnapshot) => {
      avatarLoader.failed(snapshot);
    },
    [avatarLoader],
  );

  return useMemo(
    () => ({
      view,
      details,
      avatar,
      failed,
      notices,
      loginVisible,
      loginFailed,
      sessionExpired,
      modelSignInRequired,
      showLogin,
      start,
      cancel,
      signOut,
      hasRunningAccountTasks,
      refresh: refreshAll,
      bonusShown: controller.shown,
      bonusDismissed: controller.dismiss,
      avatarFailed,
      clearSessionExpired: () => setSessionExpired(0),
      clearModelSignInRequired: () => setModelSignInRequired(0),
    }),
    [
      view, details, avatar, failed, notices, loginVisible, loginFailed, sessionExpired,
      modelSignInRequired, showLogin, start, cancel, signOut, hasRunningAccountTasks,
      refreshAll, controller, avatarFailed,
    ],
  );
}

/** Whether the projection names an attempt this UI is still waiting on. */
export function accountActive(view: AccountView | null): boolean {
  const attempt = view?.attempt;
  return attempt !== null && attempt !== undefined && ACCOUNT_ACTIVE_PHASES.includes(attempt.phase);
}
