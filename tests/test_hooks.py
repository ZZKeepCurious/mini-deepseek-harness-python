"""第 12 章测试：hooks 桥 —— CC 钩子翻译成拦截决策。"""
import json
import unittest

from miniharness.protocol.hooks import (
    ClaudeCodeBridge,
    matches_matcher,
    matcher_diagnostic,
    merge_hook_outputs,
    parse_claude_code_config,
    parse_hook_output,
    run_hook,
    substitute_command,
)
from miniharness.core.session import Session


def fake_run(exit_code=0, stdout="", stderr="", expected_event=None):
    """构造可注入的 run_fn：模拟"执行 + 解析输出"（与上游 runHook 语义一致）。"""
    def run_fn(hook, payload):
        return parse_hook_output(exit_code, stdout, stderr, expected_event), 5
    return run_fn


class TestCodec(unittest.TestCase):
    def test_exit_zero_plain_stdout_stays_text(self):
        out = parse_hook_output(0, "hello world", "")
        self.assertEqual(out["stdout"], "hello world")
        self.assertNotIn("decision", out)

    def test_exit_two_blocks_with_stderr_reason(self):
        out = parse_hook_output(2, "", "blocked because unsafe")
        self.assertEqual(out["decision"], "block")
        self.assertEqual(out["reason"], "blocked because unsafe")

    def test_malformed_json_on_clean_exit_lenient(self):
        out = parse_hook_output(0, "{not json", "")
        self.assertEqual(out["stdout"], "{not json")
        self.assertNotIn("decision", out)

    def test_structured_approve_top_level(self):
        out = parse_hook_output(0, '{"decision": "approve", "reason": "ok"}', "")
        self.assertEqual(out["decision"], "approve")
        self.assertEqual(out["reason"], "ok")

    def test_out_of_band_deny_top_level_ignored(self):
        out = parse_hook_output(0, '{"decision": "deny"}', "")
        self.assertNotIn("decision", out)   # 顶层只有 approve/block，越界值忽略

    def test_permission_decision_overrides_top_level(self):
        out = parse_hook_output(
            0, '{"decision": "approve", "hookSpecificOutput": '
               '{"hookEventName": "PreToolUse", "permissionDecision": "deny",'
               ' "permissionDecisionReason": "no"}}', "",
            expected_event_name="PreToolUse")
        self.assertEqual(out["decision"], "deny")
        self.assertEqual(out["reason"], "no")

    def test_hook_event_name_mismatch_discards_event_fields(self):
        out = parse_hook_output(
            0, '{"hookSpecificOutput": {"hookEventName": "PostToolUse",'
               ' "permissionDecision": "deny"}}', "",
            expected_event_name="PreToolUse")
        self.assertEqual(out.get("hookEventName"), "PostToolUse")   # 判别符保留
        self.assertNotIn("decision", out)                           # 事件域字段丢弃

    def test_updated_input_parsed_not_executed(self):
        out = parse_hook_output(0, '{"hookSpecificOutput": {"hookEventName":'
                                   ' "PreToolUse", "updatedInput": {"k": 1}}}', "",
                                expected_event_name="PreToolUse")
        self.assertEqual(out["updatedInput"], {"k": 1})


class TestMatcher(unittest.TestCase):
    def test_match_all_sentinels(self):
        for sentinel in (None, "", "*"):
            self.assertTrue(matches_matcher(sentinel, "bash", "claude-code"))
            self.assertTrue(matches_matcher(sentinel, "bash", "codex"))

    def test_claude_literal_pipe_alternation(self):
        self.assertTrue(matches_matcher("Bash|Read", "Bash", "claude-code"))
        self.assertTrue(matches_matcher("Bash|Read", "Read", "claude-code"))
        self.assertFalse(matches_matcher("Bash|Read", "Write", "claude-code"))

    def test_claude_regex_unanchored(self):
        self.assertTrue(matches_matcher(r"Bash.*", "BashExec", "claude-code"))
        self.assertTrue(matches_matcher("Bash.*", "BashExec", "codex"))

    def test_codex_always_regex(self):
        # 纯字母模式在 codex 下仍是正则（非锚定）
        self.assertTrue(matches_matcher("Bash", "BashExec", "codex"))
        self.assertTrue(matches_matcher("Bash", "MyBash", "codex"))

    def test_invalid_regex_no_match(self):
        self.assertFalse(matches_matcher("([", "anything", "codex"))

    def test_matcher_diagnostic(self):
        self.assertIsNone(matcher_diagnostic("Bash|Read", "claude-code"))
        self.assertIsNone(matcher_diagnostic(r"Bash.*", "claude-code"))
        self.assertIsNone(matcher_diagnostic("Bash", "codex"))
        self.assertIsNotNone(matcher_diagnostic("([", "codex"))


class TestMerge(unittest.TestCase):
    def test_deny_wins_over_allow(self):
        merged = merge_hook_outputs([
            {"decision": "allow", "exitCode": 0},
            {"decision": "deny", "reason": "no", "exitCode": 2},
        ])
        self.assertEqual(merged["decision"], "deny")
        self.assertEqual(merged["reason"], "no")

    def test_ask_beats_allow_but_loses_to_deny(self):
        self.assertEqual(merge_hook_outputs([
            {"decision": "ask"}, {"decision": "allow"}])["decision"], "ask")
        self.assertEqual(merge_hook_outputs([
            {"decision": "deny"}, {"decision": "ask"}])["decision"], "deny")

    def test_block_and_approve_fold(self):
        self.assertEqual(merge_hook_outputs([{"decision": "block"}])["decision"], "deny")
        self.assertEqual(merge_hook_outputs([{"decision": "approve"}])["decision"], "allow")

    def test_stop_sticky_first_reason(self):
        merged = merge_hook_outputs([
            {"continue": False, "stopReason": "first"},
            {"continue": False, "stopReason": "second"},
        ])
        self.assertTrue(merged["stop"])
        self.assertEqual(merged["stopReason"], "first")

    def test_reasons_joined_for_winning_rank(self):
        merged = merge_hook_outputs([
            {"decision": "deny", "reason": "a"},
            {"decision": "deny", "reason": "b"},
        ])
        self.assertEqual(merged["reason"], "a\n\nb")

    def test_context_and_messages_accumulate(self):
        merged = merge_hook_outputs([
            {"additionalContext": "c1", "systemMessage": "m1"},
            {"additionalContext": "c2"},
        ])
        self.assertEqual(merged["additionalContext"], ["c1", "c2"])
        self.assertEqual(merged["systemMessages"], ["m1"])

    def test_empty_neutral(self):
        merged = merge_hook_outputs([])
        self.assertEqual(merged["decision"], "none")
        self.assertFalse(merged["stop"])
        self.assertEqual(merged["additionalContext"], [])


class TestConfig(unittest.TestCase):
    def test_settings_object_and_bare_map(self):
        settings = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command",
                                                         "command": "echo hi"}]}]}}
        bare = {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}
        for raw in (settings, bare):
            parsed = parse_claude_code_config(raw)
            self.assertIn("PreToolUse", parsed["config"])
            self.assertEqual(parsed["config"]["PreToolUse"][0]["hooks"][0]["command"],
                             "echo hi")

    def test_non_command_skipped(self):
        parsed = parse_claude_code_config(
            {"PreToolUse": [{"hooks": [{"type": "http", "url": "x"}]}]})
        self.assertEqual(parsed["skipped"], [{"event": "PreToolUse", "type": "http"}])
        self.assertEqual(parsed["config"], {})

    def test_substitution_applied_at_parse(self):
        parsed = parse_claude_code_config(
            {"PreToolUse": [{"hooks": [{"type": "command",
                                        "command": "${CLAUDE_PLUGIN_ROOT}/check.py "
                                                   "${CLAUDE_PROJECT_DIR}"}]}]},
            vars={"pluginRoot": "/p", "projectDir": "/w"})
        cmd = parsed["config"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(cmd, "/p/check.py /w")

    def test_substitute_unset_token_verbatim(self):
        self.assertEqual(substitute_command("${CLAUDE_PROJECT_DIR}/x", {}),
                         "${CLAUDE_PROJECT_DIR}/x")

    def test_prompt_submit_matcher_discarded(self):
        parsed = parse_claude_code_config(
            {"UserPromptSubmit": [{"matcher": "anything",
                                   "hooks": [{"type": "command", "command": "echo"}]}]})
        self.assertNotIn("matcher", parsed["config"]["UserPromptSubmit"][0])

    def test_invalid_matcher_rejects_config(self):
        with self.assertRaises(SyntaxError):
            parse_claude_code_config(
                {"PreToolUse": [{"matcher": "([", "hooks": [{"type": "command",
                                                             "command": "echo"}]}]})

    def test_timeout_parsed(self):
        parsed = parse_claude_code_config(
            {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo",
                                        "timeout": 5}]}]})
        self.assertEqual(parsed["config"]["PreToolUse"][0]["hooks"][0]["timeoutSec"], 5)


class TestBridge(unittest.TestCase):
    def _bridge(self, raw, **kw):
        return ClaudeCodeBridge(raw)

    def test_pre_tool_deny_decision(self):
        bridge = ClaudeCodeBridge({
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                          "command": "x"}]}],
        })
        run_fn = fake_run(exit_code=2, stderr="forbidden")
        decision = bridge.pre_tool("Bash", run_fn=run_fn)
        self.assertEqual(decision, {"kind": "deny", "reason": "forbidden"})

    def test_pre_tool_ask_decision(self):
        bridge = ClaudeCodeBridge({
            "PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}],
        })
        run_fn = fake_run(stdout=json.dumps({
            "hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "ask",
                                   "permissionDecisionReason": "confirm"}}),
                          expected_event="PreToolUse")
        decision = bridge.pre_tool("Bash", run_fn=run_fn)
        self.assertEqual(decision, {"kind": "ask", "reason": "confirm"})

    def test_pre_tool_allow_delegates(self):
        bridge = ClaudeCodeBridge({
            "PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}],
        })
        run_fn = fake_run(stdout='{"decision": "approve"}')
        self.assertIsNone(bridge.pre_tool("Bash", run_fn=run_fn))   # 委派 next()

    def test_matcher_selects_and_skips(self):
        calls = []
        def run_fn(hook, payload):
            calls.append(payload)
            return {"exitCode": 0, "stdout": "", "stderr": ""}, 0
        bridge = ClaudeCodeBridge({
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                          "command": "x"}]}],
        })
        bridge.pre_tool("Read", run_fn=run_fn)   # 不匹配：不执行
        bridge.pre_tool("Bash", run_fn=run_fn)   # 匹配：执行
        self.assertEqual(len(calls), 1)

    def test_pre_step_reject(self):
        bridge = ClaudeCodeBridge({
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "x"}]}],
        })
        run_fn = fake_run(exit_code=2, stderr="no prompt")
        self.assertEqual(bridge.pre_step("hello", run_fn=run_fn), {"kind": "reject"})

    def test_pre_step_allow_delegates(self):
        bridge = ClaudeCodeBridge({
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "x"}]}],
        })
        run_fn = fake_run(stdout="{}")
        self.assertIsNone(bridge.pre_step("hello", run_fn=run_fn))

    def test_stop_deny_forces_continue(self):
        bridge = ClaudeCodeBridge({
            "Stop": [{"hooks": [{"type": "command", "command": "x"}]}],
        })
        run_fn = fake_run(exit_code=2, stderr="keep going")
        result = bridge.stop(run_fn=run_fn)
        self.assertEqual(result, {"continue": True, "reason": "keep going"})

    def test_stop_continue_false_without_decision_delegates(self):
        bridge = ClaudeCodeBridge({
            "Stop": [{"hooks": [{"type": "command", "command": "x"}]}],
        })
        run_fn = fake_run(stdout='{"continue": false, "stopReason": "paused"}')
        self.assertIsNone(bridge.stop(run_fn=run_fn))   # 无权限决策 → 委派

    def test_audit_pair_logged_turn_enclosed(self):
        session = Session("hook-test")
        session.append("turn/start", {"turn": 1})
        bridge = ClaudeCodeBridge({
            "PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}],
        })
        run_fn = fake_run(stdout='{"decision": "approve"}')
        bridge.pre_tool("Bash", session=session, run_fn=run_fn)
        audit = [e for e in session.events if e["type"].startswith("hook/")]
        self.assertEqual([e["type"] for e in audit], ["hook/invoked", "hook/result"])
        self.assertEqual(audit[0]["data"]["point"], "PreToolUse")
        self.assertEqual(audit[0]["data"]["dialect"], "claude-code")
        self.assertEqual(audit[0]["data"]["handlerId"], audit[1]["data"]["handlerId"])
        self.assertEqual(audit[1]["data"]["decision"], "approve")
        self.assertEqual(audit[0]["data"]["turn"], 1)
        self.assertNotIn("surfaceOp", audit[0])   # log-only 非 surface


class TestRunHookSubprocess(unittest.TestCase):
    def test_real_subprocess_clean_exit(self):
        output, duration = run_hook("python -c \"print('ok')\"")
        self.assertEqual(output["exitCode"], 0)
        self.assertEqual(output["stdout"], "ok")
        self.assertGreaterEqual(duration, 0)

    def test_real_subprocess_block_exit_two(self):
        output, _ = run_hook("python -c \"import sys; sys.stderr.write('no'); sys.exit(2)\"")
        self.assertEqual(output["decision"], "block")
        self.assertEqual(output["reason"], "no")

    def test_timeout_exit_code_undefined(self):
        output, _ = run_hook("python -c \"import time; time.sleep(5)\"", timeout_sec=0.1)
        self.assertIsNone(output["exitCode"])
        self.assertIn("timed out", output["stderr"])


if __name__ == "__main__":
    unittest.main()