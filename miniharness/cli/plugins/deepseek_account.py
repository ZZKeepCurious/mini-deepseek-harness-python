"""web 组合条目：deepseek-account（账户 Remote 面，L3 deepseek_account）。"""


def apply(ctx, **config):
    from ...deepseek_account import install_deepseek_account

    install_deepseek_account(
        ctx,
        platform_origin=config.get("platformOrigin"),
        allow_loopback_http=config.get("allowLoopbackHttp", False),
        rewrite_browser_origin=config.get("rewriteBrowserOrigin", False),
        inference_origin=config.get("inferenceOrigin", "https://api.deepseek.com"),
        desktop_platform=config.get("desktopPlatform"),
        embedded_page_dist=config.get("embeddedPageDist", ""),
        request_headers=config.get("requestHeaders"),
        account_request_headers=config.get("accountRequestHeaders"),
        request_timeout_ms=config.get("requestTimeoutMs", 30_000),
        balance_timeout_ms=config.get("balanceTimeoutMs", 30_000),
        logout_max_retries=config.get("logoutMaxRetries", 5),
        logout_retry_delay_ms=config.get("logoutRetryDelayMs", 1_000),
        attempt_timeout_ms=config.get("attemptTimeoutMs", 600_000))