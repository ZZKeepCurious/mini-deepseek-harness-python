# Prototype · 006 部署

<p class="lead">对照 `dsh-superagent-prototype/proto-006-deployment.md`（274 行）。</p>

!!! info "延伸材料"
    论文持久化 vs prototype 单机部署见 [设计抽象 §6](design-abstract.md#sec-6-persistence)。

!!! note "原文（English）"

    # 006 Deployment

    Use this guide to install, configure and operate dsh-superagent. It covers accounts, startup checks, upgrades and troubleshooting, building on [001 Domain model](proto-001-domain.md) through [005 Panel](proto-005-panel.md).

    ## What runs

    - **A dedicated DeepSeek Harness profile**, called `superagent` in this guide. Create it from the Web profile template. It loads the bundle and serves its panel on `127.0.0.1`. Keep it separate from the profile you work in: the plugin changes process-wide `PATH` and git settings, and its process needs independent restarts and upgrades.
    - **An agent GitHub account.** The agent comments, pushes branches and opens pull requests as this account, through a GitHub CLI signed in only as that account.
    - **Optional:** a Feishu bot for chat requests.

    Plugin configuration holds the state directory and machine limits. Use the panel for everything else: Sources for the repository and accounts, Policy for allowed lists, and Execution for worker directories and commands.

    ### Permissions and isolation

    The bundle defines `dsh-superagent-worker` for Executors and `dsh-superagent-router` for the main Session. They appear in the DeepSeek Harness preset list but are intended only for dsh-superagent. Do not select them for personal Sessions or disable their configuration rows. The application needs them to create and resume its Sessions.

    Opening a blank managed Session in the Web UI can select the profile's default preset. dsh-superagent restores its role-specific preset before the first turn.

    Both presets run with `danger-full-access` as the operator's OS user and read messages from subscribed Threads. Account wrappers choose the agent's credentials by default; they do not isolate the process. A worker can execute programs by path and read the operator's home directory. For isolation, use a dedicated OS account containing only the agent's credentials.

    ## Prerequisites

    - Node.js 22.19+ or 24+, pnpm, git 2.32+, Python 3.8+ and tmux
    - GitHub CLI (`gh`) 2.48 or later. Source CLI fields must point to `gh` or `lark-cli` wrappers; the host relies on their subcommands and output formats.
    - A repository on github.com; GitHub Enterprise Server is not supported. To create a checkout per Ticket, keep a local clone from which the prepare command can add worktrees. Install `git-lfs` if that repository uses Git LFS.
    - A model provider configured for DeepSeek Harness. dsh-superagent uses the profile's default model unless the Eval or Execution settings name another.
    - For Feishu: `lark-cli` (tested with 1.0.90) and a Feishu or Lark app with a bot

    ## Accounts

    ### Agent GitHub account

    1. Create a GitHub user account for the agent and give it write access to the repository. A GitHub App cannot be the agent, because dsh-superagent reads the account's notifications.
    2. Give it a GitHub CLI that uses only its own credentials:

       ```sh
       mkdir -p ~/.local/bin
       cat > ~/.local/bin/gh-agent <<'EOF'
       #!/bin/sh
       GH_CONFIG_DIR="$HOME/.config/gh-agent" exec gh "$@"
       EOF
       chmod +x ~/.local/bin/gh-agent
       gh-agent auth login
       gh-agent api user --jq .login
       ```

       Sign in as the agent account; the last command prints its login. dsh-superagent reads the login the same way.

    ### Feishu bot (optional)

    1. Create a self-built app with the bot capability. Grant it the permissions to receive message events, read and send chat messages, and read chat members, then publish the app version.
    2. Give the bot its own `lark-cli` configuration, restricted to the bot identity, and a wrapper:

       ```sh
       LARKSUITE_CLI_CONFIG_DIR="$HOME/.lark-cli-dsh-superagent" lark-cli config init --app-id <cli_…> --app-secret-stdin
       LARKSUITE_CLI_CONFIG_DIR="$HOME/.lark-cli-dsh-superagent" lark-cli config strict-mode bot --global
       cat > ~/.local/bin/lark-bot <<'EOF'
       #!/bin/sh
       export LARKSUITE_CLI_CONFIG_DIR="$HOME/.lark-cli-dsh-superagent"
       exec lark-cli "$@"
       EOF
       chmod +x ~/.local/bin/lark-bot
       ```

       The first command reads the app secret from standard input. Use a separate configuration directory: `lark-cli` allows one profile per app id, and your personal configuration may already contain a user login for that app.

       The host refuses a CLI that can act as a user. It reads the app id, Feishu/Lark brand and bot `open_id` from the bot configuration.

       Both configurations share the keychain entry for the app secret. To remove only the bot configuration, delete its directory; do not run `lark-cli config remove`, which would affect the shared secret.

    ## Install

    Run this block once, on a machine that has no `superagent` profile yet:

    ```sh
    npm install -g @deepseek-ai/dsh@0.2.1-rc.3
    git clone <this repository> dsh-superagent && cd dsh-superagent
    pnpm install && pnpm build
    dsh superagent --from-default-profile web --dump-config > /dev/null
    dsh plugin --profile superagent add "link:$PWD"
    ```

    The pinned version above is public; other versions must satisfy `engines.dsh` in `package.json`.

    The fourth command creates `~/.dsh/profiles/superagent` from the DeepSeek Harness Web template. The fifth installs this bundle. Set `DSH_HOME` to relocate the default `~/.dsh` directory.

    Run the profile-creation command only for a new installation. If the profile already exists, that command replaces it and deletes its `cordis.patch.yml`, including machine configuration. Do not use it for upgrades.

    ## Machine configuration

    Copy `dsh-superagent-prototype/examples/profile.cordis.patch.yml` to `~/.dsh/profiles/superagent/cordis.patch.yml`, replacing that file, and replace every `<placeholder>`. For Feishu, also enable the `dsh-superagent-feishu` entry there. The `webserver` entry sets the panel's port.

    | Key | Required | Meaning |
    | --- | --- | --- |
    | `stateDir` | yes | Absolute path: the database, the lock, the command shims in `worker-bin`, the agent's git configuration and, by default, the Ticket directories in `work`. Back it up before upgrades |
    | `promptMaxChars`, `admissionContextMaxPages`, `admissionContextTimeoutMs` | no | The largest saved prompt configuration, and how many Thread pages one assessment reads within what time |

    After the first start, edit machine limits in **Settings → Deployment** or the DeepSeek Harness Plugin Manager. Changes apply without restart. `stateDir` cannot be changed there. Invalid values or unknown configuration keys prevent startup; the log identifies the key.

    ## Start and stop

    ```sh
    scripts/dsh-superagent start
    scripts/dsh-superagent status
    ```

    `start` runs `dsh --profile superagent` in tmux session `dsh-superagent`, or `dsh-<profile>` for another profile. It waits up to one minute for the runtime to become ready or report a setup requirement. On a first run it prints the missing setup information. It refuses duplicate instances, including ones started outside this script.

    `stop` and `restart` request graceful shutdown and wait up to five minutes for Executors. They never force-kill the process or touch other tmux sessions.

    `status` prints process ids, health and setup problems. For a different profile or port, use `--profile` and `--url`, or set `DSH_SUPERAGENT_PROFILE` and `DSH_SUPERAGENT_URL`.

    `status`, `start` and `restart` exit with one of four codes:

    | Code | Meaning |
    | --- | --- |
    | 0 | The service runs normally |
    | 1 | It does not run, runs twice, does not answer, or its runtime reports an error; a restart may fix it |
    | 2 | It runs, but a part other than the runtime, such as a Source or a Policy sync (`sync:<source id>`), reports an error; a restart does not fix it |
    | 3 | It runs and waits for setup; a restart does not fix it |

    A Source that is still starting counts as normal operation. So does a Feishu bot that setup could not check.

    `status` returns failed parts and their details under `errors`, and setup requirements under `setup`. `start` and `restart` print those problems as lines; they exit 1 if the service never comes up. Only an intentional `stop` pauses automatic recovery.

    Each profile stores logs and control files under `.artifacts/`:

    - Startup logs: `dsh-superagent-<profile>-<date>-<time>-<pid>.log`, where the pid belongs to the script. The newest 20 are kept.
    - Control files: `control/profiles/<profile>/`, containing the stop marker, lock and process id.

    Open the URL printed in the log, then choose **Superagent** in the sidebar.

    ### Hourly check

    Create the hourly check in the DeepSeek Harness profile you normally use, not in `superagent`. It must keep running when the agent service is down. Ask an agent in that profile to create an hourly schedule using this prompt, with the checkout path filled in:

    ```text
    Check dsh-superagent: run `scripts/dsh-superagent --profile superagent status` in <checkout>. Exit 0: do nothing and say nothing. Exit 2: do not restart; tell me each part under `errors` with its detail, unless you already told me about the same ones. Exit 3: do not restart; tell me what `setup` says setup needs, unless you already told me. If it reports manually_stopped: true, leave it stopped. Otherwise (exit 1) run `scripts/dsh-superagent --profile superagent --respect-stop restart`, which exits the same way: 0 means it recovered, so say nothing; 2 and 3 are reported as above; if it exits 1 or fails otherwise, read the newest .artifacts/dsh-superagent-superagent-*.log and the Troubleshooting section of proto-006-deployment.md, fix what you can, and tell me what was wrong and what you did.
    ```

    `--respect-stop` preserves an intentional stop. Only `stop` writes the marker; `start` clears it. `restart` writes no marker, so the check can recover a restart whose shutdown timed out.

    ## Settings

    Setup must succeed before polling or work starts. Overview lists missing requirements and links to their settings. A settings change runs setup again. If you fix something outside the application, such as CLI authentication, choose **Check again**.

    1. **Sources.** Set the GitHub repository (`owner/name`) and agent CLI, for example `/home/me/.local/bin/gh-agent`. Requests normally arrive through the agent account's notifications. A new Issue or comment mentioning the account arrives on the next poll; adding a mention by editing an existing Issue body does not. Post a new comment instead. For Feishu, set the bot CLI, chats, each chat's receive-all or mentions-only mode, and whether to receive direct messages.
    2. **Policy.** The two example rules can be edited or removed. Under **On each Source**, add the GitHub accounts and Feishu users who may ask for work; until a list has someone, no request can become a Ticket. With Feishu, a chat can be chosen to sync the list from, at startup and on **Sync now**.
    3. **Execution.** The directory root and a prepare command. To give each Ticket a checkout of the repository:

       ```sh
       git -C /home/me/src/widgets fetch --no-tags "https://github.com/$DSH_SUPERAGENT_GITHUB_REPOSITORY.git" "${DSH_SUPERAGENT_GITHUB_PR_HEAD:-HEAD}:refs/dsh-superagent/$DSH_SUPERAGENT_TICKET" &&
       git -C /home/me/src/widgets worktree add -b "agent/$DSH_SUPERAGENT_NAME" "$DSH_SUPERAGENT_DIR" "refs/dsh-superagent/$DSH_SUPERAGENT_TICKET"
       ```

       List personal CLIs, such as one signed in to your own account, as blocked commands.
    4. **Prompts.** Set the team sections: the GitHub disclosure line, review bots, the commit author and the auto-merge condition.

    ### Management API

    The panel uses a local HTTP API that agents can also call. `SETUP.md` uses it to save settings, and the service script reads `GET state`.

    Send requests to `http://127.0.0.1:<port>/orchestrator/api/` with `x-orchestrator-request: 1`. The host must be `127.0.0.1:<port>` or `localhost:<port>`. Missing headers or an `Origin` from another site receive 403.

    API saves have the panel's permissions. They can change execution environment and setup account fields, unlike saves made through the main Session's tools.

    Agents may rely on these routes and fields. The other routes in `src/app/api.ts` serve the panel and change with it.

    | Request | Answer |
    | --- | --- |
    | `GET state` | What every page reads: `setup`, with `problem` and `feishuProblem`, each `null` or a setup problem with `place`, `code`, `values` and `detail`, and `accounts` once setup resolved them; `health`, an entry per part, below, with `state` (`starting`, `ready`, `paused` or `error`), `detail` and `at`; `load`, the Executor Sessions `running`, the Tickets `queued` and `maxWorkers`; `paused`; and `masterId` |
    | `GET settings` | `blocks`: `policy`, `eval` and `executor`, each with its `value` and its `revision`; `sources`: the Source instances, each with `id`, `kind`, `installed`, its block's `value`, `{"enabled": …, "config": {…}}`, and `revision`, its kind's form, and its setup `problem` when it has one; `policyVersion`; and `environment`, the execution environment's `root` and `variables` |
    | `GET sources` | Each Source's status: `state` (`missing`, `off`, `error`, `setup`, `starting`, `paused` or `ready`), the `health` of its parts, its setup `problem`, the last day's `stats`, the Triggers it `filtered` in that day, and the messages it `dropped` in that day, newest drop first, each with its `channel`, `preview`, `ticket` (`id` and `title`) when it was for one, its `thread` when the store has it, and `dropped.at` and `dropped.reason`, `channelGone` or `channelIgnored` |
    | `PUT settings/<block>` with `{"value": …, "revision": n}` | Saves one block: `saved`, the block's new `revision`, `policy_changed` and `restarting`. `value` is the whole block, as `GET settings` holds it; a missing key takes its default |
    | `POST executor/pause` with `{"paused": true}` or `false` | Pauses or resumes processing: `paused` |
    | `POST setup/check` | Runs setup again by restarting dsh-superagent: `restarting: true`. Refused when setup has nothing to fix |
    | `POST policy-sync/<source id>` | Syncs that Source's allowed list now: `policy`, the Source's part as saved |
    | `GET prompts`, `PUT prompts` with `{"config", "baseRevision"}` | Reads the prompt configuration: `current`, with its `revision` and `config`, `defaults`, `sections`, the team sections in the order Sessions read them, `skillName`, the pattern of a team skill's name, `builtin`, the names a team skill cannot take, and `skillPrefix`, which Sessions load a skill's name under. Or saves it: the new `revision` |
    | `POST tickets/<id>/retry`, `POST tickets/<id>/stop` | Retries or stops a Ticket, as the panel does |

    Health entries are grouped by responsibility:

    | Entry | Reports |
    | --- | --- |
    | `runtime` | The application itself |
    | `models` | Sessions waiting on a model-budget retry |
    | `evals` | Unavailable Evals from the last hour with no later judgement or dismissal |
    | `assessment`, `launch`, `delivery`, `relay`, `notice`, `reply` | Stored work described in [003 Execution](proto-003-execution.md); delivery counts waiting work, while reply covers Channel messages |
    | `worker` | Executor Session event handling; appears after its first failure |
    | `github`, `feishu`, `history` | Source polling, events and history recovery |
    | `sync:<source id>` | Allowed-list synchronization |

    A refusal has a status and `error`, its English text:

    - 400: an invalid value (`issues`, each with its `path`), a failed account check (`setup`), a fixed refusal (`code` and `values`), or a body that is not JSON or is too large.
    - 404: something that does not exist.
    - 409: a stale revision.
    - 500: anything else.

    The blocks:

    - `source:github`, `source:feishu`: `{"enabled": true, "config": {…}}`. GitHub's config has `repository` and `cli`, and the switches `observeIssues`, `observePulls`, `observeComments` and `observeCi`, off by default, which receive every new Issue, new pull request and comment in the repository and the default branch's CI; Feishu's has `cli`, `chats` as `[{"chatId": "oc_…", "name": "…", "listen": "mentions"}]` (or `"all"`), `directMessages`, and `otherChats` as `"none"` or `"mentions"`.
    - `policy`: `{"rules": […], "sources": {"github": {"allowed": [{"id": "<login>"}], "bots": [{"id": "dependabot[bot]"}]}, "feishu": {"allowed": [{"id": "ou_…", "name": "…"}], "syncChat": "oc_…", "admission": {"oc_…": {"unlistedAddressed": "explicit"}}}}}`. `bots` lists the bots whose messages may become inferred candidates. An `admission` row is keyed by chat id, `other` or `direct` for Feishu, and by `issues`, `pulls`, `comments` or `ci` for GitHub; an unset cell takes its default (see [002 Authorization](proto-002-authorization.md)).
    - `eval`: `mode`, `timeoutSeconds` and `model`.
    - `executor`: `model`, `maxWorkers`, `directory`, `prepare` and `blockedCommands`. The processing pause is a setting of its own, `paused`, which `POST executor/pause` sets.

    Changing a repository or CLI runs the corresponding setup check first. A failed check returns 400 with details in `setup`. A successful account change, or any save while setup is pending, returns `restarting: true`. The API is temporarily unavailable while the application restarts.

    ```sh
    api=http://127.0.0.1:3082/orchestrator/api
    rev=$(curl -s -H 'x-orchestrator-request: 1' $api/settings | jq '.sources[] | select(.id == "github") | .revision')
    curl -s -X PUT -H 'x-orchestrator-request: 1' -H 'content-type: application/json' $api/settings/source:github \
      -d "{\"value\": {\"enabled\": true, \"config\": {\"repository\": \"acme/widgets\", \"cli\": \"$HOME/.local/bin/gh-agent\"}}, \"revision\": $rev}"
    ```

    ## Setup checks

    Setup uses the DeepSeek Harness subprocess service, as worker shells do. It removes environment variables whose names contain KEY, PASSWORD, SECRET or TOKEN.

    Setup verifies the GitHub repository and the agent's push access, writes the worker environment, and checks that git uses only the agent credentials for github.com. A Feishu setup failure disables only the Feishu Source.

    The process environment that setup writes:

    - **Command shims** in `<stateDir>/worker-bin`, first on `PATH`: `gh` runs the agent's CLI; `lark-cli` runs the bot's CLI; blocked commands fail. A shim removes its own directory from `PATH` before it runs its target.
    - **git configuration** in `<stateDir>/worker-gitconfig`: replaces the operator's global and system git configuration. It selects the agent CLI as the github.com credential helper, rewrites GitHub SSH addresses to HTTPS and defines Git LFS filters. Personal git settings such as an HTTP proxy do not apply. No commit identity is set here, git never prompts for credentials, and `GH_TOKEN` and `GITHUB_TOKEN` are removed.

    A file in `worker-bin` or `worker-gitconfig` that dsh-superagent did not write stops setup. A Ticket directory inside a git repository whose own configuration adds credentials fails the Ticket.

    ## Try it

    From an allowed GitHub account, open an Issue or post a comment containing `@<agent login> <request>`. Within a polling interval (one minute by default), Overview should show a Ticket. The agent acknowledges the request, works in its own Session and directory, and delivers in the original conversation. For Feishu, mention the bot in a configured chat.

    ## Upgrade

    1. Wait until no Executor is in the middle of a turn.
    2. Back up `<stateDir>/orchestrator.sqlite`.
    3. Run `git pull && pnpm install && pnpm build`.
    4. Run `scripts/dsh-superagent restart` and inspect its status.

    A restart ends running Assignments and starts replacements with recovery notes. Team text and overridden prompt parts are retained; parts without overrides follow the new defaults. Review parts marked **Default updated** after upgrading.

    A database migrated by a newer version cannot be opened by an older one. Rollback therefore requires the pre-upgrade backup. After upgrading DeepSeek Harness itself, restart the running instance so the panel loads matching artifacts.

    ### Agent presets

    The worker and router presets move shell commands to the background after 5 seconds. A Session cannot load without its preset. Keep the `dsh-superagent-worker` and `dsh-superagent-router` rows in `cordis.patch.yml` while their Sessions are in use; otherwise resume fails with `agent-preset/not-found`.

    ## Troubleshooting

    Setup problems appear on the overview with a link to the fix, in the panel's language; the log and the API show the English text below.

    | Symptom | Cause and fix |
    | --- | --- |
    | `The agent GitHub CLI is not set.` | Set the agent's CLI on the GitHub card |
    | `The agent GitHub CLI was not found.` | Set the wrapper's absolute path, or a command name on `PATH` |
    | `The agent GitHub CLI cannot read its account` | Sign the wrapper in with `auth login` |
    | `@… cannot read …` or `@… cannot push to …` | Give the agent account write access to the repository |
    | `The GitHub Source names no repository.` | Set the repository, `owner/name`, on the GitHub card |
    | A Ticket fails with `The prepare command failed: …` | The output names the cause, such as a wrong clone path; fix the command, then retry the Ticket |
    | `git does not read the github.com credential configuration dsh-superagent writes` | Install git 2.32 or later |
    | A Ticket fails with `The git configuration of the repository in … sets …` | Remove the named credential or `http.*.extraheader` settings from the clone's `.git/config` |
    | `… was not written by dsh-superagent` | Move the named file out of `<stateDir>/worker-bin` or `<stateDir>/worker-gitconfig`, or choose another `stateDir` |
    | `The bot lark-cli allows user identities` | Point the Feishu Source at a bot-only configuration, as in Feishu bot above |
    | `The bot lark-cli does not work.` or `The bot identity of app … is …` | Check the configuration's app id and secret |
    | `The Feishu bot is not a member of the chat.` | Add the bot to the chat in the chat's settings; the next attempt succeeds |
    | `status` exits 2 | A part reports an error, which a restart does not fix: `errors` names each part, as listed under Management API, with its detail; each part clears its own entry once it works again |
    | `status` exits 3 | Setup waits: `setup` says what is missing, and the overview links to the setting that fixes it |
    | `status` exits 1 | The service does not run, runs twice, does not answer, or its runtime reports an error; the output and the newest log say which |
    | `… timed out after 90 s`, `… was stopped by …`, `… exited with code …` or `… could not run: …` | A CLI failed without printing why; run the named program by hand as the operator's user to see its own message |
    | The panel says dsh-superagent is not answering | It is restarting, or did not start because of its configuration; the DeepSeek Harness log names the cause |
    | `dsh-superagent configuration has keys it does not use` (log) | Remove the named keys; the message says where each value lives now |
    | The log says the plugin is incompatible with the running dsh | Install a DeepSeek Harness version within the `engines.dsh` range |
    | The panel shows "Failed to load plugins" after a DeepSeek Harness upgrade | Restart the running instance |
    | Nothing happens after a mention | Check that the author is on the Source's allowed list, the Source is on, and processing is not paused. On GitHub, a mention added by editing an Issue's body is not received; mention the agent in a comment |

!!! quote "译文（中文）"

    # 006 部署

    用本指南安装、配置并运行 dsh-superagent。涵盖账号、启动检查、升级与排错，建立在 [001 领域模型](proto-001-domain.md) 至 [005 面板](proto-005-panel.md) 之上。

    ## 运行什么

    - **专用的 DeepSeek Harness profile**：本指南中称为 `superagent`。从 Web profile 模板创建。它加载 bundle 并在 `127.0.0.1` 上提供面板。与日常工作的 profile 分开：插件会改进程级 `PATH` 与 git 设置，其进程需要独立重启与升级。
    - **Agent 的 GitHub 账号。** Agent 以此账号评论、推送分支、开 pull request，通过仅以此账号登录的 GitHub CLI。
    - **可选：** 用于聊天请求的飞书 bot。

    插件配置保存 state 目录与机器限制。其余均在面板：**Sources** 配仓库与账号，**Policy** 配允许列表，**Execution** 配 worker 目录与命令。

    ### 权限与隔离

    Bundle 为 Executor 定义 `dsh-superagent-worker`，为主 Session 定义 `dsh-superagent-router`。它们出现在 DeepSeek Harness preset 列表中，但仅供 dsh-superagent 使用。勿在个人 Session 选用，勿禁用其配置行。应用需要它们来创建并恢复 Session。

    在 Web UI 打开空白托管 Session 可能选中 profile 默认 preset。dsh-superagent 在首轮前恢复其角色专用 preset。

    两个 preset 均以操作者 OS 用户、`danger-full-access` 运行，并从已订阅 Thread 读消息。账号包装器默认选用 agent 凭据；并不隔离进程。Worker 可按路径执行程序并读取操作者主目录。若要隔离，请使用仅含 agent 凭据的专用 OS 账号。

    ## 先决条件

    - Node.js 22.19+ 或 24+、pnpm、git 2.32+、Python 3.8+ 与 tmux
    - GitHub CLI（`gh`）2.48 或更高。Source 的 CLI 字段须指向 `gh` 或 `lark-cli` 包装器；宿主依赖其子命令与输出格式。
    - github.com 上的仓库；不支持 GitHub Enterprise Server。若每个 Ticket 要一份 checkout，保留本地 clone，供 prepare 命令添加 worktree。仓库用 Git LFS 则安装 `git-lfs`。
    - 为 DeepSeek Harness 配置的模型提供商。dsh-superagent 使用 profile 默认模型，除非 Eval 或 Execution 设置指定其他模型。
    - 飞书：`lark-cli`（测试版本 1.0.90）及带 bot 能力的飞书或 Lark 应用

    ## 账号

    ### Agent GitHub 账号

    1. 为 agent 创建 GitHub 用户并赋予仓库写权限。GitHub App 不能作 agent，因为 dsh-superagent 读取账号通知。
    2. 为其配置仅使用自身凭据的 GitHub CLI：

       ```sh
       mkdir -p ~/.local/bin
       cat > ~/.local/bin/gh-agent <<'EOF'
       #!/bin/sh
       GH_CONFIG_DIR="$HOME/.config/gh-agent" exec gh "$@"
       EOF
       chmod +x ~/.local/bin/gh-agent
       gh-agent auth login
       gh-agent api user --jq .login
       ```

       以 agent 账号登录；最后一条命令打印其 login。dsh-superagent 以同样方式读取 login。

    ### 飞书 bot（可选）

    1. 创建带 bot 能力的自建应用。授予接收消息事件、读写聊天消息、读取聊天成员等权限，然后发布应用版本。
    2. 为 bot 配置独立的 `lark-cli`（限制为 bot 身份）及包装器：

       ```sh
       LARKSUITE_CLI_CONFIG_DIR="$HOME/.lark-cli-dsh-superagent" lark-cli config init --app-id <cli_…> --app-secret-stdin
       LARKSUITE_CLI_CONFIG_DIR="$HOME/.lark-cli-dsh-superagent" lark-cli config strict-mode bot --global
       cat > ~/.local/bin/lark-bot <<'EOF'
       #!/bin/sh
       export LARKSUITE_CLI_CONFIG_DIR="$HOME/.lark-cli-dsh-superagent"
       exec lark-cli "$@"
       EOF
       chmod +x ~/.local/bin/lark-bot
       ```

       第一条命令从标准输入读取 app secret。使用独立配置目录：`lark-cli` 每个 app id 只允许一份 profile，个人配置可能已有该 app 的用户登录。

       宿主拒绝能以用户身份操作的 CLI。它从 bot 配置读取 app id、飞书/Lark 品牌与 bot `open_id`。

       两份配置共享 keychain 中的 app secret。若只删 bot 配置，删除其目录即可；勿运行 `lark-cli config remove`，会影响共享 secret。

    ## 安装

    在尚无 `superagent` profile 的机器上运行一次：

    ```sh
    npm install -g @deepseek-ai/dsh@0.2.1-rc.3
    git clone <this repository> dsh-superagent && cd dsh-superagent
    pnpm install && pnpm build
    dsh superagent --from-default-profile web --dump-config > /dev/null
    dsh plugin --profile superagent add "link:$PWD"
    ```

    上述固定版本为公开版；其他版本须满足 `package.json` 中的 `engines.dsh`。

    第四条命令从 DeepSeek Harness Web 模板创建 `~/.dsh/profiles/superagent`。第五条安装本 bundle。设置 `DSH_HOME` 可迁移默认 `~/.dsh` 目录。

    profile 创建命令仅用于新装。若 profile 已存在，该命令会替换并删除其 `cordis.patch.yml`（含机器配置）。升级时勿用。

    ## 机器配置

    将 `dsh-superagent-prototype/examples/profile.cordis.patch.yml` 复制到 `~/.dsh/profiles/superagent/cordis.patch.yml`（覆盖原文件），并替换所有 `<placeholder>`。飞书还需启用其中的 `dsh-superagent-feishu` 项。`webserver` 项设置面板端口。

    | 键 | 必填 | 含义 |
    | --- | --- | --- |
    | `stateDir` | 是 | 绝对路径：数据库、锁、`worker-bin` 中的命令 shim、agent 的 git 配置，以及默认情况下 `work` 中的 Ticket 目录。升级前请备份 |
    | `promptMaxChars`、`admissionContextMaxPages`、`admissionContextTimeoutMs` | 否 | 最大已保存 prompt 配置，以及一次 assessment 在限定时间内读取的 Thread 页数 |

    首次启动后，在 **Settings → Deployment** 或 DeepSeek Harness Plugin Manager 中编辑机器限制。更改无需重启即生效。`stateDir` 不能在那里修改。无效值或未知配置键会阻止启动；日志会指出键名。

    ## 启停

    ```sh
    scripts/dsh-superagent start
    scripts/dsh-superagent status
    ```

    `start` 在 tmux 会话 `dsh-superagent`（或其他 profile 的 `dsh-<profile>`）中运行 `dsh --profile superagent`。最多等待一分钟直至 runtime 就绪或报告 setup 需求。首次运行会打印缺失的 setup 信息。拒绝重复实例，包括本脚本外启动的实例。

    `stop` 与 `restart` 请求优雅关闭，最多等待 Executor 五分钟。从不强杀进程，也不动其他 tmux 会话。

    `status` 打印进程 id、健康与 setup 问题。其他 profile 或端口用 `--profile`、`--url`，或设置 `DSH_SUPERAGENT_PROFILE`、`DSH_SUPERAGENT_URL`。

    `status`、`start`、`restart` 以四种退出码之一结束：

    | 码 | 含义 |
    | --- | --- |
    | 0 | 服务正常运行 |
    | 1 | 未运行、重复运行、无响应或 runtime 报错；重启可能修复 |
    | 2 | 在运行，但 runtime 以外的部分（如 Source 或 Policy 同步 `sync:<source id>`）报错；重启不能修复 |
    | 3 | 在运行并等待 setup；重启不能修复 |

    仍在启动的 Source 算正常运行。setup 无法检查的飞书 bot 亦然。

    `status` 在 `errors` 下返回失败部分及详情，在 `setup` 下返回 setup 需求。`start` 与 `restart` 将这些 problem 逐行打印；服务若始终起不来则退出 1。仅 intentional `stop` 会暂停自动恢复。

    每个 profile 在 `.artifacts/` 下存日志与控制文件：

    - 启动日志：`dsh-superagent-<profile>-<date>-<time>-<pid>.log`，pid 属于脚本。保留最新 20 个。
    - 控制文件：`control/profiles/<profile>/`，含 stop 标记、锁与进程 id。

    打开日志中打印的 URL，在侧栏选择 **Superagent**。

    ### 每小时检查

    在你常用的 DeepSeek Harness profile（非 `superagent`）中创建每小时检查。Agent 服务 down 时它仍须运行。让该 profile 中的 agent 用下列 prompt 创建 hourly schedule，并填入 checkout 路径：

    ```text
    Check dsh-superagent: run `scripts/dsh-superagent --profile superagent status` in <checkout>. Exit 0: do nothing and say nothing. Exit 2: do not restart; tell me each part under `errors` with its detail, unless you already told me about the same ones. Exit 3: do not restart; tell me what `setup` says setup needs, unless you already told me. If it reports manually_stopped: true, leave it stopped. Otherwise (exit 1) run `scripts/dsh-superagent --profile superagent --respect-stop restart`, which exits the same way: 0 means it recovered, so say nothing; 2 and 3 are reported as above; if it exits 1 or fails otherwise, read the newest .artifacts/dsh-superagent-superagent-*.log and the Troubleshooting section of proto-006-deployment.md, fix what you can, and tell me what was wrong and what you did.
    ```

    `--respect-stop` 保留 intentional stop。仅 `stop` 写标记；`start` 清除。`restart` 不写标记，以便检查可在关闭超时的 restart 后恢复。

    ## 设置

    Setup 成功后方开始轮询与工作。Overview 列出缺失项并链到相应设置。设置变更会再次运行 setup。若在应用外修复（如 CLI 认证），选择 **Check again**。

    1. **Sources。** 设置 GitHub 仓库（`owner/name`）与 agent CLI，例如 `/home/me/.local/bin/gh-agent`。请求通常经 agent 账号通知到达。新 Issue 或提及该账号的评论在下次 poll 到达；编辑已有 Issue 正文添加 mention 不会。请发新评论。飞书：设置 bot CLI、聊天、各聊天的全收或仅 mention 模式，以及是否收私信。
    2. **Policy。** 两条示例规则可编辑或删除。在 **On each Source** 下添加可发起工作的 GitHub 账号与飞书用户；列表为空则任何请求不能成为 Ticket。飞书可选某聊天在启动与 **Sync now** 时同步列表。
    3. **Execution。** 目录根与 prepare 命令。为每个 Ticket 提供仓库 checkout 示例：

       ```sh
       git -C /home/me/src/widgets fetch --no-tags "https://github.com/$DSH_SUPERAGENT_GITHUB_REPOSITORY.git" "${DSH_SUPERAGENT_GITHUB_PR_HEAD:-HEAD}:refs/dsh-superagent/$DSH_SUPERAGENT_TICKET" &&
       git -C /home/me/src/widgets worktree add -b "agent/$DSH_SUPERAGENT_NAME" "$DSH_SUPERAGENT_DIR" "refs/dsh-superagent/$DSH_SUPERAGENT_TICKET"
       ```

       将个人 CLI（如登录你自己账号的）列为 blocked commands。
    4. **Prompts。** 设置团队节：GitHub 披露行、review bot、commit author 与 auto-merge 条件。

    ### 管理 API

    面板使用本地 HTTP API，agent 也可调用。`SETUP.md` 用它保存设置，服务脚本读取 `GET state`。

    请求发往 `http://127.0.0.1:<port>/orchestrator/api/`，带头 `x-orchestrator-request: 1`。Host 须为 `127.0.0.1:<port>` 或 `localhost:<port>`。缺头或来自其他站的 `Origin` 返回 403。

    API 保存与面板权限相同。可改执行环境与 setup 账号字段，不同于经主 Session 工具保存。

    Agent 可依赖这些路由与字段。`src/app/api.ts` 中其余路由服务面板并随面板变化。

    | 请求 | 响应 |
    | --- | --- |
    | `GET state` | 各页读取：`setup`（`problem`、`feishuProblem` 各为 `null` 或含 `place`、`code`、`values`、`detail` 的 setup problem；setup 解析后还有 `accounts`）；`health`（每部分一条，含 `state`：`starting`、`ready`、`paused` 或 `error`，及 `detail`、`at`）；`load`（Executor Session `running`、Ticket `queued` 与 `maxWorkers`）；`paused`；`masterId` |
    | `GET settings` | `blocks`：`policy`、`eval`、`executor`，各含 `value` 与 `revision`；`sources`：各 Source 实例含 `id`、`kind`、`installed`、块 `value` `{"enabled": …, "config": {…}}` 与 `revision`、种类表单、setup `problem`（若有）；`policyVersion`；`environment` 执行环境 `root` 与 `variables` |
    | `GET sources` | 各 Source 状态：`state`（`missing`、`off`、`error`、`setup`、`starting`、`paused` 或 `ready`）、部分 `health`、setup `problem`、近一日 `stats`、该日 `filtered` 的 Trigger、该日 `dropped` 的消息（最新 drop 在前，各含 `channel`、`preview`、若有 Ticket 则 `ticket`（`id`、`title`）、store 有则 `thread`，及 `dropped.at`、`dropped.reason`、`channelGone` 或 `channelIgnored`） |
    | `PUT settings/<block>` 体 `{"value": …, "revision": n}` | 保存一块：`saved`、新 `revision`、`policy_changed`、`restarting`。`value` 为整块，同 `GET settings`；缺键取默认 |
    | `POST executor/pause` 体 `{"paused": true}` 或 `false` | 暂停或恢复处理：返回 `paused` |
    | `POST setup/check` | 通过重启 dsh-superagent 再跑 setup：`restarting: true`。setup 无待修项时拒绝 |
    | `POST policy-sync/<source id>` | 立即同步该 Source 允许列表：返回 `policy`、该 Source 部分如所存 |
    | `GET prompts`、`PUT prompts` 体 `{"config", "baseRevision"}` | 读 prompt 配置：`current`（`revision`、`config`）、`defaults`、`sections`（Session 读取顺序的团队节）、`skillName`（团队 skill 名模式）、`builtin`（团队 skill 不可用名）、`skillPrefix`（Session 加载 skill 名前缀）。或保存：新 `revision` |
    | `POST tickets/<id>/retry`、`POST tickets/<id>/stop` | 与面板相同重试或停止 Ticket |

    Health 条目按职责分组：

    | 条目 | 报告 |
    | --- | --- |
    | `runtime` | 应用自身 |
    | `models` | 等待模型预算重试的 Session |
    | `evals` | 近一小时内 unavailable 且无后续 judgement 或 dismissal 的 Eval |
    | `assessment`、`launch`、`delivery`、`relay`、`notice`、`reply` | [003 执行](proto-003-execution.md) 中的 stored work；delivery 计等待工作，reply 覆盖 Channel 消息 |
    | `worker` | Executor Session 事件处理；首次失败后出现 |
    | `github`、`feishu`、`history` | Source 轮询、事件与历史恢复 |
    | `sync:<source id>` | 允许列表同步 |

    拒绝含 status 与英文 `error`：

    - 400：无效值（`issues` 各含 `path`）、账号检查失败（`setup`）、固定拒绝（`code` 与 `values`）、或非 JSON/过大 body。
    - 404：不存在。
    - 409：revision 过期。
    - 500：其他。

    块结构：

    - `source:github`、`source:feishu`：`{"enabled": true, "config": {…}}`。GitHub 配置含 `repository`、`cli`，及默认关闭的 `observeIssues`、`observePulls`、`observeComments`、`observeCi`（接收仓库内每个新 Issue、新 PR、评论与默认分支 CI）；飞书含 `cli`、`chats` 如 `[{"chatId": "oc_…", "name": "…", "listen": "mentions"}]`（或 `"all"`）、`directMessages`、`otherChats` 为 `"none"` 或 `"mentions"`。
    - `policy`：`{"rules": […], "sources": {"github": {"allowed": [{"id": "<login>"}], "bots": [{"id": "dependabot[bot]"}]}, "feishu": {"allowed": [{"id": "ou_…", "name": "…"}], "syncChat": "oc_…", "admission": {"oc_…": {"unlistedAddressed": "explicit"}}}}}`。`bots` 列出消息可成 inferred candidate 的 bot。`admission` 行键为 chat id、飞书 `other`/`direct`，或 GitHub 的 `issues`、`pulls`、`comments`、`ci`；未设单元取默认（见 [002 授权](proto-002-authorization.md)）。
    - `eval`：`mode`、`timeoutSeconds`、`model`。
    - `executor`：`model`、`maxWorkers`、`directory`、`prepare`、`blockedCommands`。处理暂停为独立设置 `paused`，由 `POST executor/pause` 设置。

    改仓库或 CLI 先跑对应 setup 检查。失败返回 400，`setup` 含详情。账号改成功，或 setup pending 时任意保存，返回 `restarting: true`。应用重启期间 API 暂不可用。

    ```sh
    api=http://127.0.0.1:3082/orchestrator/api
    rev=$(curl -s -H 'x-orchestrator-request: 1' $api/settings | jq '.sources[] | select(.id == "github") | .revision')
    curl -s -X PUT -H 'x-orchestrator-request: 1' -H 'content-type: application/json' $api/settings/source:github \
      -d "{\"value\": {\"enabled\": true, \"config\": {\"repository\": \"acme/widgets\", \"cli\": \"$HOME/.local/bin/gh-agent\"}}, \"revision\": $rev}"
    ```

    ## Setup 检查

    Setup 使用 DeepSeek Harness 子进程服务，与 worker shell 相同。它移除名称含 KEY、PASSWORD、SECRET 或 TOKEN 的环境变量。

    Setup 验证 GitHub 仓库与 agent 推送权限，写入 worker 环境，并检查 git 对 github.com 仅使用 agent 凭据。飞书 setup 失败仅禁用飞书 Source。

    Setup 写入的进程环境：

    - **命令 shim** 在 `<stateDir>/worker-bin`，`PATH` 最前：`gh` 跑 agent CLI；`lark-cli` 跑 bot CLI；blocked 命令失败。Shim 运行目标前从 `PATH` 移除自身目录。
    - **git 配置** 在 `<stateDir>/worker-gitconfig`：替换操作者全局与系统 git 配置。将 agent CLI 选为 github.com 凭据 helper，GitHub SSH 改 HTTPS，定义 Git LFS filter。个人 git 设置（如 HTTP 代理）不适用。此处不设 commit 身份，git 不 prompt 凭据，并移除 `GH_TOKEN`、`GITHUB_TOKEN`。

    `worker-bin` 或 `worker-gitconfig` 中非 dsh-superagent 写入的文件会阻止 setup。Ticket 目录若位于自行添加凭据的 git 仓库内会导致 Ticket 失败。

    ## 试跑

    从允许的 GitHub 账号开 Issue 或发含 `@<agent login> <request>` 的评论。在一个 poll 间隔内（默认一分钟）Overview 应出现 Ticket。Agent 确认请求，在独立 Session 与目录中工作，并在原对话投递。飞书：在已配置聊天中 @ bot。

    ## 升级

    1. 等到没有 Executor 处于 turn 中途。
    2. 备份 `<stateDir>/orchestrator.sqlite`。
    3. 运行 `git pull && pnpm install && pnpm build`。
    4. 运行 `scripts/dsh-superagent restart` 并检查 status。

    重启结束运行中的 Assignment 并以 recovery note 启动替代。团队文本与已覆盖 prompt 部分保留；未覆盖部分跟随新默认。升级后查看标 **Default updated** 的部分。

    较新版本迁移过的数据库不能被旧版打开。回滚因此需要升级前备份。升级 DeepSeek Harness 本身后，重启运行中的实例以便面板加载匹配 artifact。

    ### Agent preset

    Worker 与 router preset 在 5 秒后将 shell 命令转后台。无 preset 无法加载 Session。Session 使用中须保留 `cordis.patch.yml` 的 `dsh-superagent-worker` 与 `dsh-superagent-router` 行；否则 resume 失败并报 `agent-preset/not-found`。

    ## 排错

    Setup 问题在 overview 以面板语言显示并链到修复；日志与 API 显示下列英文。

    | 现象 | 原因与修复 |
    | --- | --- |
    | `The agent GitHub CLI is not set.` | 在 GitHub 卡片设置 agent CLI |
    | `The agent GitHub CLI was not found.` | 设置包装器绝对路径，或 `PATH` 上的命令名 |
    | `The agent GitHub CLI cannot read its account` | 用 `auth login` 登录包装器 |
    | `@… cannot read …` 或 `@… cannot push to …` | 赋予 agent 账号仓库写权限 |
    | `The GitHub Source names no repository.` | 在 GitHub 卡片设置仓库 `owner/name` |
    | Ticket 失败 `The prepare command failed: …` | 输出指明原因（如 clone 路径错误）；修命令后重试 Ticket |
    | `git does not read the github.com credential configuration dsh-superagent writes` | 安装 git 2.32 或更高 |
    | Ticket 失败 `The git configuration of the repository in … sets …` | 从 clone 的 `.git/config` 移除指明的凭据或 `http.*.extraheader` |
    | `… was not written by dsh-superagent` | 将所指文件移出 `<stateDir>/worker-bin` 或 `<stateDir>/worker-gitconfig`，或换 `stateDir` |
    | `The bot lark-cli allows user identities` | 飞书 Source 指向仅 bot 配置，见上文飞书 bot |
    | `The bot lark-cli does not work.` 或 `The bot identity of app … is …` | 检查配置的 app id 与 secret |
    | `The Feishu bot is not a member of the chat.` | 在聊天设置中将 bot 加入聊天；下次尝试成功 |
    | `status` 退出 2 | 某部分报错，重启不能修：`errors` 列出各部分（同管理 API）及 detail；恢复后各部分自行清除条目 |
    | `status` 退出 3 | 等待 setup：`setup` 说明缺失项，overview 链到修复设置 |
    | `status` 退出 1 | 服务未运行、重复、无响应或 runtime 报错；输出与最新日志说明原因 |
    | `… timed out after 90 s`、`… was stopped by …`、`… exited with code …` 或 `… could not run: …` | CLI 失败且未打印原因；以操作者用户手动运行所指程序查看其消息 |
    | 面板称 dsh-superagent 无响应 | 正在重启，或配置导致未启动；DeepSeek Harness 日志指明原因 |
    | `dsh-superagent configuration has keys it does not use`（日志） | 删除所指键；消息说明各值现处位置 |
    | 日志称插件与运行中 dsh 不兼容 | 安装 `engines.dsh` 范围内的 DeepSeek Harness 版本 |
    | DeepSeek Harness 升级后面板显示 "Failed to load plugins" | 重启运行中的实例 |
    | mention 后无反应 | 检查作者在 Source 允许列表、Source 已开、处理未暂停。GitHub：编辑 Issue 正文添加的 mention 不会收到；请在评论中 @ agent |
