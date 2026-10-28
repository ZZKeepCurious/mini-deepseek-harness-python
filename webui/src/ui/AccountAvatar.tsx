// Shared account picture with an icon fallback for missing or unavailable
// images (upstream ui-settings-account/src/client/AccountAvatar.tsx).

import type { AccountAvatarSnapshot } from "../app/accountAvatar";

export interface AccountAvatarProps {
  avatar: AccountAvatarSnapshot | undefined;
  /** @param snapshot - picture attempt whose rendered image failed. */
  avatarFailed: (snapshot: AccountAvatarSnapshot) => void;
}

export function AccountAvatar({ avatar, avatarFailed }: AccountAvatarProps) {
  return avatar?.status === "ready" ? (
    <img
      className="account-avatar-image"
      src={avatar.url}
      alt=""
      referrerPolicy="no-referrer"
      onError={() => {
        avatarFailed(avatar);
      }}
    />
  ) : (
    <span className="account-avatar-fallback" aria-hidden="true">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <circle cx="8" cy="5" r="3" stroke="currentColor" strokeWidth="1.2" />
        <path
          d="M2.5 14c0-2.8 2.5-4.6 5.5-4.6s5.5 1.8 5.5 4.6"
          stroke="currentColor"
          strokeWidth="1.2"
          strokeLinecap="round"
        />
      </svg>
    </span>
  );
}
