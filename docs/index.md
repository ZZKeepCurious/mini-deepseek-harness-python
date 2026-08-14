# Mini DeepSeek Harness（Python）— 文档入口

> 用纯 Python 标准库从 0 到 1 复现 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 核心能力的教学项目。
> 仓库：https://github.com/ZZKeepCurious/mini-deepseek-harness-python

## 分析报告（主文档）

- **[《DeepSeek Harness 深度掌握指南与技术报告》](report/DEEPSEEK-HARNESS-DEEP-LEARNING-GUIDE.html)** —— 对上游仓库的深度剖析：五层架构、ctx 服务地图、技术核心、关键流程（含 Mermaid 图，需联网加载 CDN）。

## 教程手册（step-by-step）

- [00 环境准备](chapters/00-setup.md)
- [01 事件溯源会话](chapters/01-event-sourced-session.md)
- [02 插件上下文与事件总线](chapters/02-plugin-context-and-event-bus.md)
- [03 工具执行管线](chapters/03-tool-execution-pipeline.md)
- [04 Agent Loop + LLM 流式](chapters/04-agent-loop-and-llm-streaming.md)
- [05 持久化 + 崩溃恢复 + 组合](chapters/05-persistence-recovery-composition.md)
- [06 进阶接缝](chapters/06-advanced-seams.md)

> 手册内 Mermaid 图在 GitHub Pages 上不会渲染（显示为代码块），完整效果请查看分析报告或本地 IDE 预览。