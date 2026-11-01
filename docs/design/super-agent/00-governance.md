# 显式采纳治理

## 默认规则

本目录全部 `SA-*` 原则与解读 **对 mini 无约束力**。仅当某原则写入 [adopted.md](adopted.md)，并在工作区 `status/mini-harness/tasks.md` 或仓库根 `ROADMAP.md` 中链到该 ID 后，才在该立项范围内参与设计与验收。

未在 adopted 中登记的内容，**不得**作为「mini 漏做 superagent 功能」的依据。

## 原则编号

| 前缀 | 来源 |
|------|------|
| `SA-A-xx` | 论文各节 |
| `SA-P-xx` | prototype 000–001（哲学与领域） |
| `SA-R-xx` | prototype 002–007（授权、执行、提示、面板、部署、架构） |
| `SA-S-xx` | 综合卷跨源凝练 |

ID 全库唯一；汇总表见 [principles.md](principles.md)。

## 显式采纳最小记录

写入 [adopted.md](adopted.md) 时每条须含：

| 字段 | 含义 |
|------|------|
| 原则 ID | 如 `SA-A-04` |
| 范围 | 例：`webui 通知`、`headless 编排`、`全产品 UX` |
| 采纳依据 | ROADMAP 段落 / tasks 条 / migration-log 验收步骤 |
| 检验口径 | 可走查或测试的一句话（来自 principles 表） |

闭合开发任务时：migration-log 记验收；若原则废止，在 adopted 标 Superseded 并可选写入 [rejected.md](rejected.md) 理由。

## 与 A 线审校差异

`docs/design/super-agent/` 适用工作区 `DOCS-REVIEW-SOP.md` 门 2–6；**门 1** 变体：论断须锚定论文章节或 `dsh-superagent-prototype/docs/` 路径。涉及 mini 或 DSH 行为时再锚 `miniharness/` 或 `deepseek-harness/packages/`（详见工作区 `status/mini-harness/design-superagent-track.md`）。

**门 7（译文）**：

- **论文卷** `paper-01` … `paper-06`：`!!! note` 英文与 `!!! quote` 中文按论文章节一一对应；合并小节须在页内标明（如 paper-05 的 §5–6）。
- **Prototype** `proto-000`–`008`：与旁路 `docs/00x-*.md` 小节一一对应的英中 inline 块（见 [sources.md](sources.md#sec-translation-granularity)）。旁路更新时须同步本页；**不得**用节级摘要替代全文 inline。

## 文档维护

- **解读单一事实源**：[design-abstract.md](design-abstract.md)（流程、架构 Mermaid、对照表、与 mini 边界）。
- **延伸页**（`paper-*` / `proto-*`）：保留原文与译文；页首 `!!! info "延伸材料"` 链主文对应 `#sec-*` 节；**不**在延伸页重复维护解读块或与主文同题的 Mermaid，避免漂移。
- **原则表**：[principles.md](principles.md) 为 `SA-*` 检验口径真源；[design-abstract §9](design-abstract.md#sec-9-principles) 为叙事摘要，二者冲突以 principles 表为准。
