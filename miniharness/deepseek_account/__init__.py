"""deepseek-account 域（Service Definition + 本地/Platform 实现）。

上游 `packages/credentials/deepseek-account/src/index.ts`——抽象
`DeepSeekAccount` 服务（getState/getProfile/getBalance/startSignIn/
cancelSignIn/signOut/watch/resolveToken/rejectToken/getPlatformSession）。
浏览器 PKCE 与 Platform HTTP 实现在 `deepseek-account-platform`：
`platform.py` 承载 attempt 状态机、`/oauth/callback` 与 Platform 档案/余额/
grant 存储。

`install_deepseek_account` 在存在 `credentials` + `authorization`（或显式
`platform_origin`）时装配 `PlatformAccount`；否则退回 `LocalAccountService`
这一诚实默认（signed-out、profile/balance 为 null、startSignIn fail loud）。

dsh-v0.2.0-rc.2 增补：`resolve_token(base_url)` / `reject_token(token)` 凭据
seam，以及 `deepseek-account/signed-out` / `session-expired` /
`model-sign-in-required` 事件。

类型（types.ts:5-79）：AccountView（status/links/attempt）、SignInAttemptView、
AccountLinks、AccountProfile、AccountWallet、AccountDetails、SignInErrorCode、
AccountClientMetadata、AccountBonusBatch/Notification。
"""
from __future__ import annotations

from typing import Any

from ..core.scope import Context, Service
from ..llm.protocol import ACCOUNT_SIGN_IN_REQUIRED

__all__ = [
    "AccountLinks",
    "DeepSeekAccount",
    "LocalAccountService",
    "SIGN_IN_ERROR_CODES",
    "install_deepseek_account",
    "is_running_account_task",
]

#: 安全失败码闭集（types.ts:17）。
SIGN_IN_ERROR_CODES = ("no-response", "network", "protocol", "expired", "storage")


def AccountLinks() -> dict:
    """浏览器目的地（无 token；缺省空 URL——Host 平台配置缺失）。"""
    return {"usageUrl": "", "topUpUrl": ""}


class DeepSeekAccount(Service):
    """`ctx.deepseekAccount` 服务定义（index.ts:32-117）。"""

    provide = "deepseekAccount"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "deepseekAccount")

    def get_state(self) -> dict:
        raise NotImplementedError

    def get_profile(self, client: dict | None = None):
        raise NotImplementedError

    def get_balance(self, client: dict | None = None):
        raise NotImplementedError

    def start_sign_in(self, client: dict, callback_origin: str,
                      login_source: str) -> dict:
        raise NotImplementedError

    def cancel_sign_in(self, attempt_id: str) -> dict:
        raise NotImplementedError

    def sign_out(self) -> dict:
        raise NotImplementedError

    def resolve_token(self, base_url: str) -> str | None:
        """为推断来源解析一个请求凭据；非允许 origin 或未登录返回 None（index.ts:99）。"""
        raise NotImplementedError

    def reject_token(self, token: str) -> None:
        """推断请求拒绝某 token 时移除仍匹配的本地凭据并发布过期通知（index.ts:105）。"""
        raise NotImplementedError


class LocalAccountService(DeepSeekAccount):
    """本地落空实现：无浏览器 grant → 缺省恒 signed-out。

    上游默认（未登录）即 `status='signed-out'`；本实现诚实返回该态，
    getProfile/getBalance → null（无 grant）。startSignIn 因无浏览器 PKCE
    载体 fail loud（`network` 码——发起即失败，如实报告无授权通道）。
    一个已存储的凭据可经 `token=` 注入（宿主/测试载体），此时 resolve_token
    返回它、sign_out/reject_token 清除它并发布对应事件。
    """

    def __init__(self, ctx: Context, token: str | None = None):
        super().__init__(ctx)
        self._token = token

    def get_state(self) -> dict:
        return {"status": "credential-stored" if self._token is not None else "signed-out",
                "links": AccountLinks(), "attempt": None}

    def get_profile(self, client: dict | None = None):
        return None

    def get_balance(self, client: dict | None = None):
        return None

    def resolve_token(self, base_url: str) -> str | None:
        return self._token

    def reject_token(self, token: str) -> None:
        if self._token is None or token != self._token:
            return
        self._token = None
        self.ctx.emit("deepseek-account/session-expired")

    def start_sign_in(self, client: dict, callback_origin: str,
                      login_source: str) -> dict:
        raise RuntimeError(
            "deepseek-account: browser sign-in is unavailable in this deployment "
            "(no browser PKCE carrier mounted)")

    def cancel_sign_in(self, attempt_id: str) -> dict:
        return self.get_state()

    def sign_out(self, client: dict | None = None) -> dict:
        self._token = None
        self.ctx.emit("deepseek-account/signed-out")
        return self.get_state()


def is_running_account_task(agent: Any) -> bool:
    """该 Agent 是否在 `deepseek-account` 路由上运行（上游 isRunningAccountTask）。

    判据：status == 'running' 且最新 request/context 的 provider 为
    `deepseek-account`（单适配器载体下账户模式适配器的 provider 值）。
    """
    if getattr(agent, "status", None) != "running":
        return False
    session = getattr(agent, "session", None)
    if session is None:
        return False
    context = session.request_context()
    return bool(context) and context.get("provider") == "deepseek-account"


def install_deepseek_account(ctx: Context, *,
                             token: str | None = None,
                             platform_origin: str | None = None,
                             allow_loopback_http: bool = False,
                             rewrite_browser_origin: bool = False,
                             inference_origin: str = "https://api.deepseek.com",
                             desktop_platform: str | None = None,
                             embedded_page_dist: str = "",
                             request_headers: dict[str, str] | None = None,
                             account_request_headers: dict[str, str] | None = None,
                             request_timeout_ms: int = 30_000,
                             balance_timeout_ms: int = 30_000,
                             logout_max_retries: int = 5,
                             logout_retry_delay_ms: int = 1_000,
                             attempt_timeout_ms: int = 600_000) -> DeepSeekAccount:
    """装配 `ctx.deepseekAccount`（Platform 或本地落空实现；幂等，重复装返回既有实例）。

    同时镜像上游 `account-tasks.ts`：某回合以 ACCOUNT_SIGN_IN_REQUIRED 失败时
    在事件总线发布 `deepseek-account/model-sign-in-required`；账户签出
    （`deepseek-account/signed-out`）时取消所有仍在 `deepseek-account` 路由上
    运行的 Agent（keep_inbox，cause `{kind:'hook', reason:'deepseek-account/signed-out'}`）。
    """
    existing = ctx.get("deepseekAccount")
    if existing is not None:
        return existing
    if platform_origin is not None or (ctx.get("credentials") is not None
                                       and ctx.get("authorization") is not None):
        from .platform import PlatformAccount
        service = PlatformAccount(
            ctx,
            platform_origin_value=platform_origin or "https://platform.deepseek.com",
            allow_loopback_http=allow_loopback_http,
            rewrite_browser_origin=rewrite_browser_origin,
            inference_origin=inference_origin,
            desktop_platform=desktop_platform,
            embedded_page_dist=embedded_page_dist,
            request_headers=request_headers,
            account_request_headers=account_request_headers,
            request_timeout_ms=request_timeout_ms,
            balance_timeout_ms=balance_timeout_ms,
            logout_max_retries=logout_max_retries,
            logout_retry_delay_ms=logout_retry_delay_ms,
            attempt_timeout_ms=attempt_timeout_ms)
    else:
        service = LocalAccountService(ctx, token=token)

    def on_agent_error(payload: Any) -> None:
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get("code") == ACCOUNT_SIGN_IN_REQUIRED:
            ctx.emit("deepseek-account/model-sign-in-required")

    def on_signed_out(_payload: Any = None) -> None:
        agents = ctx.get("agents")
        if agents is None:
            return
        for agent in agents.list():
            if is_running_account_task(agent):
                agent.cancel(
                    {"kind": "hook", "reason": "deepseek-account/signed-out"},
                    keep_inbox=True)

    ctx.on("agent/error", on_agent_error, global_=True)
    ctx.on("deepseek-account/signed-out", on_signed_out, global_=True)
    return service