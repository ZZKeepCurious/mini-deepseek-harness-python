# 出处与旁路上游

## 论文原文

| 载体 | 链接 | 说明 |
|------|------|------|
| 知乎专栏（中文发布） | https://zhuanlan.zhihu.com/p/2099987320443237060 | 标题《Super-agent 设计探索：迈向更自主的 Agent 系统》 |
| 英文全文（团队工程文） | https://offloop.org/learn/deepseek-super-agent-design | B 线论文卷 **英文原文** 主对照 |

译文以英文工程文段落为准；知乎版仅作中文用语校对。论文开篇声明：本文是架构思想，**不是** DeepSeek Harness 现有产品功能说明书。

## Prototype 旁路仓库

| 项 | 值 |
|----|-----|
| 路径（工作区） | `dsh-superagent-prototype/` |
| Remote | https://github.com/deepseek-ai/dsh-superagent-prototype.git |
| 文档 | `dsh-superagent-prototype/docs/000-philosophy.md` … `008-cookbook.md` |

旁路 commit 与更新记录见工作区 `status/upstream/superagent-baseline.md`（**不**写入 DSH 对齐基线 `baseline.md`）。

## B 线译文粒度 {#sec-translation-granularity}

读者在 nav 里看到的「论文卷 / Prototype 译文」并不都是同一深度的 inline 全文。下表说明本目录页与旁路 upstream 的关系；**系统解读与架构图只在** [design-abstract.md](design-abstract.md)，延伸页供逐句核对与查定义。

| 页面 | 本目录内容 | 完整英文以谁为准 |
|------|------------|------------------|
| [paper-01](paper-01-motivation-and-four-traits.md)–[06](paper-06-experiments-and-open.md) | 各节 `!!! note` 英文 + `!!! quote` 中文，小节一一对应 | 本页 + [offloop 工程文](https://offloop.org/learn/deepseek-super-agent-design) |
| [proto-000](proto-000-philosophy.md)–[008](proto-008-cookbook.md) | 各节 `!!! note` 英文 + `!!! quote` 中文，与旁路 `docs/00x-*.md` 小节一一对应（007 按 major `##` 分段；008 为扩展 Source 食谱） | 本页与旁路 `dsh-superagent-prototype/docs/` 同步维护；旁路 commit 见 `superagent-baseline.md` |

旁路 `docs/` 变更时：同步对应 `proto-*` 英中块，并回链 [design-abstract](design-abstract.md) 对应节；`src/` 变更时同步 [source_implement/](source_implement/index.md)。延伸页**不在**另起一套与主文/走读重复的 Mermaid（见 [00-governance.md](00-governance.md)）。

## 源码走读 source_implement {#sec-source-walkthrough}

| 项 | 约定 |
|----|------|
| 代码行为真源 | 旁路 `dsh-superagent-prototype/src/` @ `superagent-baseline.md` 记录 commit |
| mini 文档 | [source_implement/index.md](source_implement/index.md)（`container` + `lifecycle/` 1–7 + `reference-semantics`；92 文件索引） |
| Python 参照 | 仓库顶层 `superagent/README.md`（见 [superagent-framework.md](superagent-framework.md)） |
| 旁路 pull 后 | 先 diff `src/` 与 `docs/007-architecture.md`，再更新走读与 `proto-*` 译文 |

## 治理文档

- [显式采纳规则](00-governance.md)  
- [已采纳原则台账](adopted.md)  
- [讨论后不采纳](rejected.md)（可选登记）

工作区 B 线审校约定：`status/mini-harness/design-superagent-track.md`。
