"""deepseek-account 域（Service Definition + 本地落空实现）。

上游 `packages/credentials/deepseek-account/src/index.ts`（106 行）——抽象
`DeepSeekAccount` 服务（getState/getProfile/getBalance/startSignIn/
cancelSignIn/signOut/watch/resolveToken/getPlatformSession）。实际 OAuth +
Platform HTTP 实现在 `deepseek-account-platform`（浏览器 PKCE + Platform 档案/
余额 + grant 存储），是**浏览器宿主载体**。

功能对标迁移（黄金法则 #5）：服务面可移植——`ctx.deepseekAccount` 抽象 + 一个
**本地落空实现**（无浏览器 grant 时的诚实默认）：`status='signed-out'`、
getProfile/getBalance 返回 null、startSignIn 因无浏览器载体 fail loud。
浏览器 PKCE + Platform HTTP 是宿主/传输载体差异（同 browser-use），登记触发
条件：引入浏览器客户端时实现平台域。

类型（types.ts:5-47）：AccountView（status/links/attempt）、SignInAttemptView、
AccountLinks、AccountProfile、AccountWallet、AccountDetails、SignInErrorCode。
"""
from __future__ import annotations

from typing import Any

from ..core.scope import Context, Service

__all__ = [
    "AccountLinks",
    "DeepSeekAccount",
    "LocalAccountService",
    "SIGN_IN_ERROR_CODES",
    "install_deepseek_account",
]

#: 安全失败码闭集（types.ts:7）。
SIGN_IN_ERROR_CODES = ("network", "protocol", "expired", "storage")


def AccountLinks() -> dict:
    """浏览器目的地（无 token；缺省空 URL——Host 平台配置缺失）。"""
    return {"usageUrl": "", "topUpUrl": ""}


class DeepSeekAccount(Service):
    """`ctx.deepseekAccount` 服务定义（index.ts:23-77）。"""

    provide = "deepseekAccount"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "deepseekAccount")

    def get_state(self) -> dict:
        raise NotImplementedError

    def get_profile(self):
        raise NotImplementedError

    def get_balance(self):
        raise NotImplementedError

    def start_sign_in(self, locale: str, callback_origin: str,
                      login_source: str) -> dict:
        raise NotImplementedError

    def cancel_sign_in(self, attempt_id: str) -> dict:
        raise NotImplementedError

    def sign_out(self) -> dict:
        raise NotImplementedError


class LocalAccountService(DeepSeekAccount):
    """本地落空实现：无浏览器 grant → 恒 signed-out。

    上游默认（未登录）即 `status='signed-out'`；本实现诚实返回该态，
    getProfile/getBalance → null（无 grant）。startSignIn 因无浏览器 PKCE
    载体 fail loud（`network` 码——发起即失败，如实报告无授权通道）。
    """

    def get_state(self) -> dict:
        return {"status": "signed-out", "links": AccountLinks(), "attempt": None}

    def get_profile(self):
        return None

    def get_balance(self):
        return None

    def start_sign_in(self, locale: str, callback_origin: str,
                      login_source: str) -> dict:
        raise RuntimeError(
            "deepseek-account: browser sign-in is unavailable in this deployment "
            "(no browser PKCE carrier mounted)")

    def cancel_sign_in(self, attempt_id: str) -> dict:
        return self.get_state()

    def sign_out(self) -> dict:
        return self.get_state()


def install_deepseek_account(ctx: Context) -> DeepSeekAccount:
    """装配 `ctx.deepseekAccount`（本地落空实现；幂等，重复装返回既有实例）。"""
    existing = ctx.get("deepseekAccount")
    if existing is not None:
        return existing
    return LocalAccountService(ctx)