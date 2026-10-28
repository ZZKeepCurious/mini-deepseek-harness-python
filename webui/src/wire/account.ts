// `account` Remote namespace (packages/api/account-controller).
// Contract: `account/watch` streams the safe projection (current value first,
// then one frame per change); profile, balance and unnotified bonuses are
// independent reads that answer null without a stored account grant, and every
// read except getState/hasRunningAccountTasks carries AccountClientMetadata so
// the Host can derive Platform request headers. A sign-in attempt moves through
// initializing → waiting-browser → exchanging → committing → succeeded and is
// cancelled by the attempt id the projection names.

import { rpc } from "./rpc";

export interface AccountLinks {
  usageUrl: string;
  topUpUrl: string;
}

export type SignInPhase =
  | "initializing"
  | "waiting-browser"
  | "exchanging"
  | "committing"
  | "succeeded"
  | "cancelled"
  | "expired"
  | "failed";

export interface SignInAttemptView {
  id: string;
  phase: SignInPhase;
  authorizeUrl?: string;
  expiresAt?: number;
  errorCode?: string;
}

export interface AccountView {
  status: "signed-out" | "credential-stored";
  links: AccountLinks;
  attempt: SignInAttemptView | null;
}

export interface AccountProfile {
  id: string | null;
  name: string | null;
  contact: string | null;
  avatarUrl?: string | null;
}

export interface AccountWallet {
  currency: "CNY" | "USD";
  balance: string;
}

export type AccountProfileOutcome =
  | { status: "ready"; value: AccountProfile }
  | { status: "failed" };

export type AccountBalanceOutcome =
  | { status: "ready"; value: AccountWallet[]; bonusWallets: AccountWallet[] }
  | { status: "failed" };

export interface AccountDetails {
  profile: AccountProfileOutcome;
  balance: AccountBalanceOutcome;
}

export interface AccountBonusNotification {
  title?: string;
  orderId: string;
  message: string;
  /** Empty string means the award never expires. */
  expiresAt: string;
  amount?: unknown;
  currency?: unknown;
  grantedAt?: unknown;
  campaign?: unknown;
}

export interface AccountBonusBatch {
  accountId: string;
  bonuses: AccountBonusNotification[];
}

export const ACCOUNT_ACTIVE_PHASES: readonly SignInPhase[] = [
  "initializing",
  "waiting-browser",
  "exchanging",
  "committing",
];

/** Client identity the Host turns into Platform request headers. */
export function accountClientMetadata(version: string) {
  const locale =
    typeof navigator !== "undefined" && typeof navigator.language === "string" && navigator.language
      ? navigator.language
      : "en-US";
  const timezoneOffsetSeconds = -new Date().getTimezoneOffset() * 60;
  return { version, locale, timezoneOffsetSeconds };
}

export type AccountClientMetadata = ReturnType<typeof accountClientMetadata>;

export async function getAccountState(): Promise<AccountView> {
  return rpc<AccountView>("account/getState", {});
}

export async function getAccountProfile(
  client: AccountClientMetadata,
): Promise<AccountProfileOutcome | null> {
  return rpc<AccountProfileOutcome | null>("account/getProfile", { client });
}

export async function getAccountBalance(
  client: AccountClientMetadata,
): Promise<AccountBalanceOutcome | null> {
  return rpc<AccountBalanceOutcome | null>("account/getBalance", { client });
}

export async function getUnnotifiedBonuses(
  client: AccountClientMetadata,
): Promise<AccountBonusBatch | null> {
  return rpc<AccountBonusBatch | null>("account/getUnnotifiedBonuses", { client });
}

export async function ackBonusNotified(
  accountId: string,
  orderId: string,
  client: AccountClientMetadata,
): Promise<boolean> {
  return rpc<boolean>("account/ackBonusNotified", { accountId, orderId, client });
}

export async function hasRunningAccountTasks(): Promise<boolean> {
  return rpc<boolean>("account/hasRunningAccountTasks", {});
}

export async function startSignIn(
  client: AccountClientMetadata,
  callbackOrigin: string,
  loginSource: "web" | "desktop",
): Promise<AccountView> {
  return rpc<AccountView>("account/startSignIn", { client, callbackOrigin, loginSource });
}

export async function cancelSignIn(attemptId: string): Promise<AccountView> {
  return rpc<AccountView>("account/cancelSignIn", { attemptId });
}

export async function signOut(client: AccountClientMetadata): Promise<AccountView> {
  return rpc<AccountView>("account/signOut", { client });
}

/** `account/watch` endpoint name; frames carry the {@link AccountView} value. */
export const ACCOUNT_WATCH_ENDPOINT = "account/watch";
