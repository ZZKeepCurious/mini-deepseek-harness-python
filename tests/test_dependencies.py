"""步骤 4 验收：import 方向断言 + 顶层 API 收敛（对照 docs/architecture.md §3/§4）。

用 stdlib AST 静态检查 miniharness/ 包内模块的导入边，钉死分层规则：
L0 地基  = core/session、core/scope            （两者互不依赖）
L1 领域  = llm/*、core/tools、core/system_prompt、boot/*   （仅 L0）
L2 编排  = core/agent_loop、compaction、commands、goal、jobs、plan、skills   （L0 + L1）
L3 应用  = cli/*、protocol/*、seams/*、preset、extensions、interaction、client
  （L0 ~ L2）
  教学层   = demo.py、example_plugins.py（任意层，但不得被业务模块导入）

补充规则（§5）：
  - protocol/ 内 acp/sdk/hooks 互不依赖；
  - seams/ 内 sandbox / credentials / subagent 三个子域互不依赖；
  - 顶层 __all__ 收敛至 28（白名单 + FakeLlmAdapter，§6）。

上游对照：无（这是 mini 自身的架构纪律，见 docs/architecture.md §3）。
"""
import ast
import pathlib
import unittest

import miniharness

PACKAGE_ROOT = pathlib.Path(miniharness.__file__).resolve().parent

# (前缀, 层)，最长前缀优先（core 含多层，须精确到子路径）
LAYER_UNITS = [
    ("core.session", 0),
    ("core.scope", 0),
    ("core.session_store", 1),
    ("core.tools", 1),
    ("core.system_prompt", 1),
    ("llm", 1),
    ("boot", 1),
    ("core.agent_loop", 2),
    ("compaction", 2),
    ("commands", 2),
    ("goal", 2),
    ("jobs", 2),
    ("plan", 2),
    ("skills", 2),
    ("cli", 3),
    ("protocol", 3),
    ("seams", 3),
    ("preset", 3),
    ("extensions", 3),
    ("interaction", 3),
    ("client", 3),
]

TEACHING_MODULES = {"miniharness.demo", "miniharness.example_plugins"}


def _unit_of(module_path: str):
    if module_path.startswith("miniharness."):
        module_path = module_path[len("miniharness."):]
    for prefix, layer in LAYER_UNITS:
        if module_path == prefix or module_path.startswith(prefix + "."):
            return prefix, layer
    return None, None


def _resolve_import(module_name: str, level: int, target: str | None):
    """把相对/绝对导入换算成绝对模块名；非 miniharness 目标返回 None。"""
    if level == 0:
        if target and target.startswith("miniharness"):
            return target
        return None
    parts = module_name.split(".")
    base = parts[: len(parts) - level]
    if target:
        base = base + target.split(".")
    return ".".join(base)


def _collect_edges(module_name: str):
    rel = pathlib.Path(*module_name.split(".")[1:]).with_suffix(".py")
    tree = ast.parse((PACKAGE_ROOT / rel).read_text(encoding="utf-8"), filename=str(rel))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield _resolve_import(module_name, 0, alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and not (node.module or "").startswith("miniharness"):
                continue
            yield _resolve_import(module_name, node.level, node.module)


def _iter_business_modules():
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(PACKAGE_ROOT)
        name = "miniharness." + ".".join(rel.with_suffix("").parts)
        if name in TEACHING_MODULES:
            continue
        yield name, path


class ImportDirectionTest(unittest.TestCase):
    """§5 规则 1：L_n 只依赖 L_<n，禁止同层或上层。"""

    def test_layer_edges(self):
        violations = []
        for module_name, _ in _iter_business_modules():
            if module_name.endswith("__init__"):
                continue  # 聚合器再导出豁免（core/__init__ 等）
            src_unit, src_layer = _unit_of(module_name)
            if src_unit is None:
                violations.append(f"{module_name}: 不在 §5 层表内")
                continue
            for dst in _collect_edges(module_name):
                if dst is None:
                    continue
                if dst == "miniharness":
                    violations.append(f"{module_name}: 导入顶层 miniharness（教学面）")
                    continue
                if dst in TEACHING_MODULES:
                    # 教学层由 test_teaching_layer_not_imported_by_business 单独断言
                    continue
                dst_unit, dst_layer = _unit_of(dst)
                if dst_unit is None:
                    violations.append(f"{module_name}: 导入 {dst}（不在 §5 层表内）")
                    continue
                if dst_unit == src_unit:
                    continue  # 族内协作（如 agent.py ↔ tool_calls.py）合法
                if src_unit == "seams" and dst_unit == "protocol":
                    # §5 显式例外：seams.subagent 是 ACP/SDK 线协议的服务端载体，
                    # 复用 protocol 层的帧/信封实现（见 docs/architecture.md §3 规则 1）
                    continue
                if dst_layer >= src_layer:
                    violations.append(
                        f"{module_name}: L{src_layer} 导入 {dst}（L{dst_layer}，违反 §5 规则 1）"
                    )
        self.assertEqual(violations, [])

    def test_protocol_siblings_independent(self):
        """§5 规则 2：protocol/ 内 acp/sdk/hooks 互不依赖。"""
        violations = []
        for module_name, _ in _iter_business_modules():
            src_unit, _ = _unit_of(module_name)
            if src_unit != "protocol" or module_name.endswith("__init__"):
                continue
            for dst in _collect_edges(module_name):
                dst_unit, _ = _unit_of(dst or "")
                if dst_unit == "protocol" and dst != module_name:
                    violations.append(f"{module_name}: 导入同级协议模块 {dst}")
        self.assertEqual(violations, [])

    def test_seams_domains_independent(self):
        """§5 规则 3：seams/ 内 sandbox / credentials / subagent 互不依赖。"""
        domains = ("sandbox_local", "credentials_local", "subagent")
        violations = []
        for module_name, _ in _iter_business_modules():
            src_unit, _ = _unit_of(module_name)
            if src_unit != "seams" or module_name.endswith("__init__"):
                continue
            src_domain = module_name.split(".")[2]
            for dst in _collect_edges(module_name):
                dst_unit, _ = _unit_of(dst or "")
                if dst_unit != "seams":
                    continue
                dst_domain = dst.split(".")[2]
                if dst_domain in domains and dst_domain != src_domain:
                    violations.append(f"{module_name}: 跨子域导入 {dst}")
        self.assertEqual(violations, [])

    def test_teaching_layer_not_imported_by_business(self):
        """教学层（demo / example_plugins）不得被业务模块导入。"""
        violations = []
        for module_name, _ in _iter_business_modules():
            for dst in _collect_edges(module_name):
                if dst in TEACHING_MODULES:
                    if module_name == "miniharness.cli.main":
                        # 显式例外：无 profile 时 --demo 兜底（教学扩展入口，见 §5 规则 4）
                        continue
                    violations.append(f"{module_name}: 导入教学层模块 {dst}")
        self.assertEqual(violations, [])

    def test_no_dead_top_level_modules(self):
        """步骤 1 兼容 shim 已全部删除：顶层只留包目录与教学文件。"""
        top = sorted(p.name for p in PACKAGE_ROOT.iterdir() if p.is_file() and p.suffix == ".py")
        self.assertEqual(top, ["__init__.py", "demo.py", "example_plugins.py"])


# §6 白名单 + FakeLlmAdapter（步骤 3 收敛结果，28 项）
TOP_LEVEL_ALL = {
    "AgentLoop", "Context", "DeepSeekAdapter", "FakeLlmAdapter", "JsonlPersistence",
    "LlmAdapter", "LlmFailure", "PluginManager", "SESSION_FORMAT_VERSION", "Session",
    "SessionPersistence", "SqlitePersistence", "StreamChunk", "TOOL_NOT_STARTED",
    "TOOL_OUTCOME_UNKNOWN", "Tool", "ToolRegistry", "apply_patch", "boot",
    "create_message", "derive_messages", "reasoning_block", "repair_interrupted_turn",
    "run_headless", "text_block", "tool_call_block", "tool_result_block", "turn_balance",
}


class TopLevelApiTest(unittest.TestCase):
    """§6：顶层 __all__ 收敛至 28（白名单 + FakeLlmAdapter），黑名单不在列。"""

    def test_all_converged(self):
        self.assertEqual(len(miniharness.__all__), 28)
        self.assertEqual(set(miniharness.__all__), TOP_LEVEL_ALL)

    def test_blacklist_not_reexported(self):
        for name in ("deep_freeze", "is_json_safe", "load_events_checked",
                     "repair_and_replay", "balanced_after_replay"):
            self.assertNotIn(name, miniharness.__all__)
            self.assertFalse(hasattr(miniharness, name))


if __name__ == "__main__":
    unittest.main()