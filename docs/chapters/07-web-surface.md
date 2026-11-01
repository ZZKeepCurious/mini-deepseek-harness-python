[← 第 7 章导读](07-external-entry-points.md)

入口组合：[Fig S5](../system-map.md#fig-s5)。

# 第 7 章 · §7.3 web 表面

## 7.3 web：同一个 base 上的浏览器表面 {#sec-7-3}

`dsh-web-app` bundle（`packages/bundle/web-app/README.md`）在 `dsh-base` 之上叠了 Web Host 层：webserver、API 网关、workspace、投影缓存、存储，以及浏览器插件名录。它自带的 `web-startup` 提供方解析 `--host` / `--port` / `--trusted-host` 和应用自己的 `--help`，**参数解析完成前不会绑定任何端口**——所以 `dsh --profile web --help` 只是打印帮助。

web 与 headless 是同一 base 的"同级表面"（README 原文 sibling surface）：内核、工具、会话全部共享，只有 Host 层不同。web 与 headless 的差别只在组合层，第 5 章的 boot/patch 就是干这个的。

web 的宿主侧分两半，mini 对应如下：

| 半 | 上游 | 内容 | mini |
|---|---|---|---|
| HTTP + WS 载体与约定 | `packages/client/connection` + `packages/api/gateway` + `packages/api/session-controller` + `packages/api/remotes` | 两信封 RPC + unary 会话服务（`/api/<endpoint>` POST `{args}`）+ WS `/api/remote.mux` 承载 Remote 流（open/cancel/item/end/error）+ `$events` 注册表 + follow/control + 审批 waterfall 桥 + `frontend-static` 静态载体 | `miniharness/web/`（§7.5） |
| 浏览器前端 | `packages/client`（React 客户端包族） | React shell、对象层、Trajectory、审批面板等 | 产品面为顶层 `webui/`（React+TS+Vite，§7.5.4）；`web/static/` vanilla SPA 是旧 wire 的教学参照 |

web 的 HTTP/WS 传输层、静态约定与前端都在 §7.5 讲解，`--profile web` 启动它并监听地址/端口，优先级：`--host`/`--port`（`cli/main.py` 透传）> env `MINIHARNESS_WEB_HOST`/`MINIHARNESS_WEB_PORT`（缺省 `127.0.0.1` / `0`=OS 分配）。监听地址须为**具体地址**——通配（`0.0.0.0`/`::` 及等价拼写）一律拒绝（`web/host.py`，对齐上游 webserver），非回环具体地址须配 token。

