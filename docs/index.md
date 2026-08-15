# Mini DeepSeek Harness（Python）— 文档入口

> 用纯 Python 标准库从 0 到 1 复现 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 核心能力的教学项目。
> 仓库：https://github.com/ZZKeepCurious/mini-deepseek-harness-python

## 分析报告（主文档 · HTML 体系）

- **[报告首页（阅读地图）](report/index.html)** —— 体系化入口：六个主题子页导航 + 四维对照表（上游包 ↔ mini 模块 ↔ 手册章节 ↔ 报告页面）。
  - [01 项目全景与分层架构](report/01-overview.html)
  - [02 系统架构与内核](report/02-architecture.html)
  - [03 关键处理流程](report/03-flows.html)
  - [04 产品面全解读](report/04-product-surface.html)（模式设计、外部入口、Trajectory、干预面、审批、自我修改、resume、plan/goal、压缩与后台）
  - [05 路线图与 Python 复现](report/05-roadmap.html)
  - [06 附录与 HOWTO](report/06-appendix.html)
  - 旧版完整报告（归档保留）：[DEEPSEEK-HARNESS-DEEP-LEARNING-GUIDE.html](report/DEEPSEEK-HARNESS-DEEP-LEARNING-GUIDE.html)
- 图表由 Mermaid.js 渲染，需联网加载 CDN。

## 教程手册（step-by-step · 施工图纸层）

- [00 环境准备](chapters/00-setup.md)
- [01 事件溯源会话](chapters/01-event-sourced-session.md)
- [02 插件上下文与事件总线](chapters/02-plugin-context-and-event-bus.md)
- [03 工具执行管线](chapters/03-tool-execution-pipeline.md)
- [04 Agent Loop + LLM 流式](chapters/04-agent-loop-and-llm-streaming.md)
- [05 持久化 + 崩溃恢复 + 组合](chapters/05-persistence-recovery-composition.md)
- [06 进阶扩展口](chapters/06-advanced-seams.md)
- [07 外部入口](chapters/07-external-entry-points.md)
- [08 组合层深读](chapters/08-composition-layer.md)
- [09 Agent 干预面](chapters/09-agent-intervention.md)
- [10 轨迹投影引擎](chapters/10-trajectory-projection.md)
- [11 运行时自我修改](chapters/11-runtime-self-modification.md)
- [12 异步化与并行工具](chapters/12-async-parallel-tools.md)

> 手册内 Mermaid 图在 GitHub Pages 上不会渲染（显示为代码块），完整效果请查看分析报告或本地 IDE 预览。