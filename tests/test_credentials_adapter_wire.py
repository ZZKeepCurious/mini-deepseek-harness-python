"""DeepSeekAdapter ↔ ctx.credentials 接线（resolve_api_key）。"""
import os
import tempfile
import unittest
from unittest.mock import patch

from miniharness.core.scope import Context
from miniharness.llm.deepseek import DeepSeekAdapter
from miniharness.seams.credentials_local import (
    LocalCredentialProvider,
    install_credentials,
    wire_llm_adapter_credentials,
)
from miniharness.cli.headless import run_headless
from miniharness.core.tools import ToolRegistry
from miniharness.web.api import WebApi


class CredentialsAdapterWireTest(unittest.TestCase):
    def test_wire_llm_adapter_credentials(self):
        ctx = Context()
        self.addCleanup(ctx.dispose)
        with tempfile.TemporaryDirectory() as tmp:
            install_credentials(ctx, LocalCredentialProvider(
                filename=os.path.join(tmp, "credentials.json"),
                read_env=False,
            ))
            ctx.get("credentials").set("DEEPSEEK_API_KEY", "sk-managed-file")
            adapter = DeepSeekAdapter(api_key="")
            wire_llm_adapter_credentials(ctx, adapter)
            self.assertEqual(adapter._auth_headers(), {"x-api-key": "sk-managed-file"})

    def test_headless_run_wires_resolver(self):
        ctx = Context()
        self.addCleanup(ctx.dispose)
        with tempfile.TemporaryDirectory() as tmp:
            install_credentials(ctx, LocalCredentialProvider(
                filename=os.path.join(tmp, "credentials.json"),
                read_env=False,
            ))
            ctx.get("credentials").set("DEEPSEEK_API_KEY", "sk-headless")
            adapter = DeepSeekAdapter(api_key="")
            with patch("miniharness.core.agent_loop.agent.AgentLoop.followup"):
                run_headless(
                    "noop",
                    adapter=adapter,
                    ctx=ctx,
                    tools=ToolRegistry(ctx),
                    exit_fn=lambda _code: None,
                )
            self.assertEqual(adapter._auth_headers(), {"x-api-key": "sk-headless"})

    def test_web_api_installs_resolver(self):
        ctx = Context()
        self.addCleanup(ctx.dispose)
        with tempfile.TemporaryDirectory() as tmp:
            install_credentials(ctx, LocalCredentialProvider(
                filename=os.path.join(tmp, "credentials.json"),
                read_env=False,
            ))
            ctx.get("credentials").set("DEEPSEEK_API_KEY", "sk-via-web-api")
            adapter = DeepSeekAdapter(api_key="")
            WebApi(ctx, adapter)
            self.assertEqual(adapter._auth_headers(), {"x-api-key": "sk-via-web-api"})


if __name__ == "__main__":
    unittest.main()
