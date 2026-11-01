# Prototype · 008 Cookbook

<p class="lead">对照 `dsh-superagent-prototype/docs/008-cookbook.md`（426 行）。</p>

!!! info "延伸材料"
    扩展 Source 实现指南见 [设计抽象 §7](design-abstract.md#sec-7-prototype)。

## 008 Cookbook

!!! note "原文（English）"

    # 008 Cookbook

    This cookbook shows how to extend dsh-superagent. It uses the terms in [001 Domain model](proto-001-domain.md) and [002 Authorization](proto-002-authorization.md), and the components in [007 Architecture](proto-007-architecture.md).

!!! quote "译文（中文）"

    # 008 开发手册

    本手册说明如何扩展 dsh-superagent。术语见 [001 领域模型](proto-001-domain.md) 与 [002 授权](proto-002-authorization.md)，组件见 [007 架构](proto-007-architecture.md)。

## Add a Source

!!! note "原文（English）"

    ## Add a Source

    This recipe implements a Source kind called `webhook`. It reads an imaginary message queue through an invented CLI, `hookq`, and converts messages to Triggers. All other interfaces are part of dsh-superagent.

    The implementation has four responsibilities: translate events, define receiving and authorization rules, provide replies and context, and maintain a polling loop. The numbered steps build those pieces in order.

    The example assumes this CLI contract:

    - `hookq pull --after <n> --json` prints the messages after position `n`, oldest first: `{"me": "<the agent's id on the queue>", "items": […], "next": <position>}`. An item has `id`, `conversation`, `thread` (the id of the message that started its thread, absent on that message), `sender` with `id` and `bot`, `mentions_me`, `text`, `at` and `url`.
    - `hookq reply --conversation <id> --to <message id> --key <key> --text <text>` replies under a message, once per key. For a conversation that was deleted it fails with `{"error": {"code": 410, "message": "…"}}` on standard error.
    - `hookq thread --conversation <id> --root <message id> --json`, with `--after <cursor>` for a later page, prints a page of a thread, oldest first: `{"items": […], "next": "<cursor>"}`, without `next` on the last page.

    Put the kind and its plugin entry in `src/sources/webhook.ts`, following `src/sources/github.ts`. If the Source grows, split translation, Channel and polling code into separate modules as the Feishu Source does.

!!! quote "译文（中文）"

    ## 新增一种 Source

    本示例实现名为 `webhook` 的 Source 种类。它通过虚构 CLI `hookq` 读取假想消息队列，并将消息转换为 Trigger。其余接口均属 dsh-superagent。

    实现承担四项职责：翻译事件、定义接收与授权规则、提供回复与上下文、维护轮询循环。编号步骤按顺序拼装这些部分。

    示例假定如下 CLI 契约：

    - `hookq pull --after <n> --json` 打印位置 `n` 之后的消息，按时间从旧到新：`{"me": "<the agent's id on the queue>", "items": […], "next": <position>}`。每条 item 含 `id`、`conversation`、`thread`（发起该 thread 的消息 id，该消息本身无此字段）、`sender`（含 `id` 与 `bot`）、`mentions_me`、`text`、`at` 与 `url`。
    - `hookq reply --conversation <id> --to <message id> --key <key> --text <text>` 在某条消息下回复，每个 key 仅一次。对话已删除时在标准错误输出 `{"error": {"code": 410, "message": "…"}}`。
    - `hookq thread --conversation <id> --root <message id> --json`，后续页加 `--after <cursor>`，打印 thread 的一页，从旧到新：`{"items": […], "next": "<cursor>"}`，最后一页无 `next`。

    将 kind 及其插件入口放在 `src/sources/webhook.ts`，参照 `src/sources/github.ts`。若 Source 变大，可像 Feishu Source 那样把翻译、Channel 与轮询代码拆到独立模块。

## 实现步骤（1–11）

!!! note "原文（English）"

    ### 1. Map the events onto the domain

    Choose stable mappings before writing code. Trigger ids, Thread ids and Channel addresses are persisted, so changing them later can disconnect existing records.

    | Concept | For the webhook queue |
    | --- | --- |
    | Trigger | One message |
    | Thread | A message and the replies to it in one conversation: `<conversation>:<id of the first message>` |
    | Actor | The sender's id on the queue, as the allowed list names it |
    | Channel address | The conversation and the message a reply goes under |
    | Receiving scope | A conversation, with its own row in the admission table |

    ### 2. Configuration: schema, fields, facts and health

    The module imports what the steps below use:

    ```ts
    import type { Context } from '@deepseek-ai/cordis'
    import { setTimeout as delay } from 'node:timers/promises'
    import { z } from 'zod'
    import { key, threadKey, type OutboundMessage, type Trigger } from '../core/index.ts'
    import { CliError } from '../infra/commands.ts'
    import { KV } from '../kv.ts'
    import { health, sourceContext, type SourceContext } from './host.ts'
    import type { SourceKind } from './kind.ts'
    ```

    ```ts
    const ConfigSchema = z.object({
      conversations: z.array(z.object({
        id: z.string().regex(/^[a-z0-9-]+$/),
        listen: z.enum(['mentions', 'all']),
      }).strict()).default([]),
      pollSeconds: z.number().int().min(10).max(3600).default(30),
    }).strict()
    export type WebhookConfig = z.infer<typeof ConfigSchema>

    export const webhookKind: SourceKind<WebhookConfig> = {
      kind: 'webhook',
      title: { zh: 'Webhook 队列', en: 'Webhook queue' },
      description: { zh: '接收 webhook 队列里下列对话的消息。', en: 'Receives the messages of these conversations from the webhook queue.' },
      schema: ConfigSchema,
      fields: [
        { key: 'conversations', type: 'rows', label: { zh: '接收的对话', en: 'Conversations' }, columns: [
          { key: 'id', type: 'text', label: { zh: '对话', en: 'Conversation' }, pattern: '[a-z0-9-]+' },
          { key: 'listen', type: 'select', label: { zh: '接收', en: 'Receive' }, options: [
            { value: 'mentions', label: { zh: '仅 @', en: '@ only' } }, { value: 'all', label: { zh: '全部消息', en: 'All' } }] },
        ] },
        { key: 'pollSeconds', type: 'number', advanced: true, min: 10, max: 3600,
          label: { zh: '轮询间隔 / 秒', en: 'Polling interval / seconds' } },
      ],
      health: ['webhook'],
      // find, scope, channel, threadContext, policy and environment follow in the next steps.
    }
    ```

    - `kind` is the stable Source id. It appears in Trigger `source`, Thread-key prefixes, the `source:webhook` settings block and the Source Policy.
    - `schema` validates stored configuration and supplies defaults. Before an operator saves an instance, the registry creates an enabled instance from those defaults. Make defaults receive nothing; this example defaults to no conversations.
    - `fields` generates the Source settings form without custom panel code. A `rows` field edits entries keyed by its first column; `advanced` hides less common settings. Implement `refreshLabels` if the Source can resolve ids to display names. Save those names under `KV.sources.labels(kind)`. Columns with `choose` and `refresh` can then offer them.
    - Optional `facts(deployment)` displays values derived by setup, such as the account behind a CLI. Setup currently checks only GitHub and Feishu fields listed in `SETUP_FIELDS`, so this example supplies no facts.
    - `health` declares the Source's health entries, initially `starting`. Registration claims each name and refuses collisions with entries such as `runtime`, `reply`, `worker` or effect kinds. Use the kind's name and reserve `sync:<id>` for Policy syncs. An `error` makes the status script exit 2 and the hourly check notify the operator, so report it only when human action may be needed.

    ### 3. Translate a message into a Trigger

    Validate CLI responses before converting them:

    ```ts
    /** One message as `hookq pull` and `hookq thread` print it. */
    const MessageSchema = z.looseObject({
      id: z.string().min(1), conversation: z.string().min(1), thread: z.string().optional(),
      sender: z.looseObject({ id: z.string(), bot: z.boolean().default(false) }), mentions_me: z.boolean().default(false),
      text: z.string(), at: z.string(), url: z.string().optional(),
    })
    type QueueMessage = z.infer<typeof MessageSchema>
    /** The answer of `hookq pull`, whose items are read one by one, and of `hookq thread`. */
    const PageSchema = z.looseObject({ me: z.string(), items: z.array(z.unknown()), next: z.number().int() })
    const ThreadPageSchema = z.looseObject({ items: z.array(MessageSchema), next: z.string().optional() })
    ```

    ```ts
    /** The Trigger of one queue message; the agent's own messages produce none. */
    export function webhookTrigger(message: QueueMessage, me: string): Trigger | undefined {
      if (message.sender.id === me) return undefined
      const thread = `${message.conversation}:${message.thread ?? message.id}`
      return {
        id: key('webhook', message.id),
        source: 'webhook',
        threads: [{ source: 'webhook', id: thread }],
        actor: { source: 'webhook', id: message.sender.id },
        human: !message.sender.bot,
        ...(message.mentions_me ? { addressed: 'mention' as const } : {}),
        at: message.at,
        text: message.text,
        channel: { source: 'webhook', thread: `webhook:${thread}`,
          address: { conversation: message.conversation, message: message.id, ...(message.url ? { url: message.url } : {}) } },
        ...(message.url ? { url: message.url } : {}),
        meta: { conversation: message.conversation, messageId: message.id, root: message.thread ?? message.id },
      }
    }
    ```

    - **Stable ids.** Derive the Trigger id from the queue id through `key`. `Engine.receive` returns `duplicate` for stored ids, and duplicate accepts create only one Ticket. Re-reading messages is therefore safe. To treat edits as new events, include an edit version in the id, as GitHub does with comment `updated_at`.
    - **Threads.** The first Thread is primary and owns the Eval. Additional Threads add Correlation context. The Source chooses the id; its full key is `webhook:<id>`.
    - **Sender.** Use Source-native ids in `actor`. Set `human: false` for bots and CI. Listed bots can only become inferred candidates. An empty actor id, as for CI, uses the admission cell for unlisted, unaddressed senders. Set `addressed` to `mention` or `direct` only when the message actually addresses the agent; this gives an allowed person's message explicit strength. Never produce a Trigger for the agent's own message.
    - **Reply destination and metadata.** `channel` identifies where replies go; `url` links to the source message. `channel.thread` groups messages for ordered delivery: a Ticket's later messages wait behind its earlier ones until an earlier message has failed three times. Other Tickets do not wait. Keep Source-specific fields in `meta`, such as the receiving conversation and Thread root. Both `meta` and `text` are untrusted data, not authorization.

    Agents can read the new kind through `orchestrator_read trigger/<id>` and `thread/<key>`. These expose message text, disposition and neighboring Triggers. A new kind does not automatically receive a built-in skill, resources for original service records or pull-request binding.

    Text search works without extra code. Implement `find(query)` when people use Source-specific references such as message ids, URLs or `#12`. Return the Thread keys and search terms those references identify:

    ```ts
      find: query => ({ threads: [], terms: [...new Set(query.match(/\bm[0-9]+\b/g) ?? [])] }),
    ```

    Here a queue message id in the query, such as `m42`, searches the Triggers for it.

    ### 4. What the Source receives: scope

    ```ts
      scope({ meta }, config) {
        const conversation = config.conversations.find(item => item.id === meta.conversation)
        if (!conversation) return { listened: false, reason: 'conversation_not_listed' }
        return { listened: true, observed: conversation.listen === 'all', row: conversation.id }
      },
    ```

    The Engine calls `scope` through `SourceRegistry` for each incoming Trigger. Out-of-scope messages are stored as filtered with their reason and create no Thread. Pass messages to `receive` rather than silently dropping them in the polling loop, so the panel can explain filtering.

    `observed: true` means the Source receives unaddressed messages in that scope, allowing admission-table decisions for them. `row` identifies the corresponding admission row.

    Revalidation calls `scope` with the Mandate's stored metadata. Make it depend only on `meta` and configuration. Resuming requires `listened`, not `observed`. Without `scope`, a kind receives every Trigger it produces but observes none.

    ### 5. Replies: the Channel

    ```ts
      channel: {
        allowed: (address, config) => config.conversations.some(item => item.id === address.conversation),
        send: sendWebhook,
        link: address => ({ url: address.url ?? '',
          label: { zh: `Webhook 对话 ${address.conversation}`, en: `Webhook conversation ${address.conversation}` } }),
      },
    ```

    ```ts
    export async function sendWebhook(host: Pick<SourceContext, 'commands'>, message: OutboundMessage): Promise<'sent' | 'gone'> {
      const { conversation, message: to } = message.channel.address
      if (!conversation || !to) throw new Error('Missing webhook reply target')
      try {
        await host.commands.run('hookq', ['reply', '--conversation', conversation, '--to', to, '--key', message.id,
          '--text', message.text])
        return 'sent'
      } catch (error) {
        if (error instanceof CliError && error.code === 410) return 'gone'
        throw error
      }
    }
    ```

    - **Idempotent sends.** Outbox calls `SourceRegistry.send`, which invokes `channel.send`. Messages wait while the Source plugin is unloaded. A send may fail after the remote service received it, so retries must not duplicate messages. Use `message.id` as an idempotency key: `hookq` supports `--key`; GitHub stores a hidden comment marker; Feishu accepts an idempotency key.
    - **Permanent versus retryable failure.** Return `sent` on success and `gone` only when delivery is permanently impossible, such as the example's 410. A gone message is retained as dropped and never queued again; it appears in `GET sources` and on the Source page. Throw for other failures so Outbox retries after 5 seconds, doubling to 300 seconds. `CliError.code` is the code reported by the CLI output.
    - **Allowed destinations.** `allowed(address, config)` checks only the saved address and current configuration. Put every required fact in the address; for example, Feishu records whether the chat is direct. A destination no longer listened to is dropped, while a disabled Source leaves messages waiting. Without `allowed`, all addresses are permitted while the Source is enabled.
    - **Links.** `link(address, links)` returns the URL and label the panel displays through `SourceRegistry.link`.

    ### 6. Thread context for assessments

    ```ts
      threadContext: {
        messageId: trigger => String(trigger.meta.messageId ?? ''),
        // A queue thread is flat: its root, and the message a reply answers, are on its first page, as on GitHub.
        read: async (commands, trigger, _part, cursor) => {
          const page = ThreadPageSchema.parse(JSON.parse(await commands.run('hookq', ['thread', '--conversation',
            String(trigger.meta.conversation), '--root', String(trigger.meta.root), '--json', ...(cursor ? ['--after', cursor] : [])])))
          const messages = page.items.map(item => ({ id: item.id, from: item.sender.id, at: item.at, text: item.text,
            ...(item.sender.bot ? { bot: true as const } : {}), ...(item.url ? { url: item.url } : {}) }))
          return { messages, ...(page.next ? { next: page.next } : {}), body: { messages } }
        },
      },
    ```

    Proactive assessment needs the conversation around a Trigger. `orchestrator_eval` reads an initial window; `orchestrator_eval_read` expands pages, the root or the parent message. Implement the optional `threadContext` interface to provide these reads:

    - `read(commands, trigger, part, cursor, config)` returns a `ContextPart`. Its `messages` use `ContextMessage`: `{id, from, at, text}` plus applicable `bot`, `deleted`, `reply_to`, `url`, `path` and `line` fields. `next` is omitted on the last page. `body` is the content shown to the assessing Session, here the same messages. Optional `status` can describe special results such as Feishu's `chat_neighbours`. `part` is `thread`, `root` or `parent`.
    - `messageId(trigger)` finds the Trigger's own message among them.
    - `images(text)` and `image(…)` are for a kind whose messages carry images, as Feishu's do; the example leaves them out.

    A kind without `threadContext` gives assessments no Thread context; the Trigger's own text stays readable.

    If messages reply to earlier messages, add `repliesTo(trigger)`. It returns the corresponding Trigger id, allowing follow-up notices and worker Inputs to show the replied-to message when stored. For example, if `hookq` supplies `reply_to` and translation stores it as `meta.replyTo`:

    ```ts
      repliesTo: trigger => typeof trigger.meta.replyTo === 'string' ? key('webhook', trigger.meta.replyTo) : undefined,
    ```

    ### 7. Who may ask: the Source's part of the Policy

    ```ts
      policy: {
        schema: z.object({ allowed: z.array(z.object({ id: z.string().min(1), name: z.string().trim().max(100).optional() }).strict())
          .default([]) }).strict(),
        fields: [{ key: 'allowed', type: 'rows', label: { zh: '能发起工作的发送者', en: 'Senders who may originate work' }, columns: [
          { key: 'id', type: 'text', label: { zh: '发送者', en: 'Sender' } },
          { key: 'name', type: 'text', label: { zh: '名字', en: 'Name' }, optional: true }] }],
        explicit: { zh: '名单里的人在消息里 @ 智能体。', en: 'Someone on the list mentions the agent in a message.' },
        rows: stored => ConfigSchema.parse(stored).conversations.map(conversation => {
          const all = conversation.listen === 'all'
          return { id: conversation.id, label: conversation.id, note: all ? { zh: '全部消息', en: 'All' } : { zh: '仅 @', en: '@ only' },
            cells: { listedUnaddressed: all, unlistedAddressed: true, unlistedUnaddressed: all } }
        }),
      },
    ```

    - `schema` validates the Source Policy, including `allowed` sender ids. Id comparison is case-insensitive. Admission tables are validated separately and shared across kinds. This Policy part is saved at `policy.sources.webhook` and contributes to the Policy version.
    - `fields` generates the form under **Eval → On each Source**, beginning with `allowed`. `explicit` explains how to address the agent. Optional `unnamed: {person, bot}` supplies display labels for opaque ids such as Feishu's `ou_…`; this example omits it because queue ids are readable names.
    - `rows(config)` lists admission scopes and their available cells. Each row id must match the `row` returned by `scope`. Unaddressed-message cells are available only where the Source observes those messages. Kinds without `rows` use default cells.
    - Optional `bots` lists bots eligible for inferred candidacy, using the same entry shape as `allowed`. Without it, no bot is admitted.
    - Optional `sync: {label, ready(part), run(host, part, signal)}` refreshes membership at startup and on **Sync now**. Feishu uses chat membership. Failures appear under `sync:<id>` and retry after one minute, doubling to one hour until successful.

    ### 8. Variables for the Executor

    ```ts
      environment: {
        variables: [{ key: 'DSH_SUPERAGENT_WEBHOOK_CONVERSATION', description: {
          zh: '接下工单的 webhook 对话；工单不是从这个来源来的时为空',
          en: 'The webhook conversation that started the ticket; empty when the ticket did not start on this Source' } }],
        values: ({ trigger }) => ({
          DSH_SUPERAGENT_WEBHOOK_CONVERSATION: trigger?.source === 'webhook' ? String(trigger.meta.conversation ?? '') : '' }),
      },
    ```

    Execution settings show declared `variables` beside the prepare command even when the Source is disabled. While enabled, `values` supplies Ticket-specific values using deployment, configuration and the originating Trigger.

    The prepare command reads environment variables; the worker reads their values in its instructions. Use the `DSH_SUPERAGENT_<KIND>_…` prefix, return an empty value for Tickets from another Source, and never include credentials.

    ### 9. The plugin entry and its loop

    ```ts
    export const name = 'dsh-superagent-webhook'
    export const inject = ['orchestrator']
    export function apply(ctx: Context): void {
      ctx.effect(() => ctx.orchestrator.sources.register(webhookKind), 'webhook: source kind')
      ctx.effect(() => {
        const source = sourceContext(ctx.orchestrator, webhookKind)
        const controller = new AbortController()
        // DSH ends its process on an unhandled rejection, so a failure that escapes the loop becomes the Source's health.
        const running = watch(source, controller.signal)
          .catch((error: unknown) => { source.report('webhook', { state: 'error', detail: String(error) }) })
        return async () => { controller.abort(); await running }
      })
    }
    ```

    The plugin injects `orchestrator`. Its first effect registers the kind and unregisters it on disposal. The second runs the polling loop with a `SourceContext`. Disposal aborts the loop and awaits completion, including during application stop or restart.

    ```ts
    async function watch(ctx: SourceContext, signal: AbortSignal): Promise<void> {
      await ctx.ready
      // Setup restarts dsh-superagent once it resolves the accounts; until then the Source reads nothing.
      if (!ctx.configured()) { ctx.report('webhook', health('paused', 'setupPending')); return }
      let failures = 0
      while (!signal.aborted) {
        const config = ctx.config<WebhookConfig>('webhook')
        if (!config) ctx.report('webhook', health('paused', 'sourceOff'))
        else {
          const at = new Date().toISOString()
          try {
            const { read, unreadable } = await pollQueue(ctx, signal)
            failures = 0
            ctx.report('webhook', { state: 'ready', detail: `${read} messages, ${unreadable} unreadable`, at })
          } catch (error) {
            failures += 1
            // One network failure is retried on the next round; three in a row need a person.
            if (!signal.aborted && failures >= 3) {
              ctx.report('webhook', health('error', 'pollFailures', { count: String(failures), error: String(error) }))
            }
          }
        }
        await delay((config?.pollSeconds ?? 30) * 1000, undefined, { signal }).catch(() => {})
      }
    }

    /** Read the queue after the cursor, take in each message, then move the cursor past them. */
    export async function pollQueue(ctx: SourceContext, signal: AbortSignal): Promise<{ read: number; unreadable: number }> {
      const after = ctx.state.get(KV.webhook.cursor) ?? 0
      const output = await ctx.commands.withSignal(signal).run('hookq', ['pull', '--after', String(after), '--json'])
      const page = PageSchema.parse(JSON.parse(output))
      let unreadable = 0
      for (const item of page.items) {
        if (signal.aborted) throw new Error('The webhook Source stopped during the read')
        const message = MessageSchema.safeParse(item)
        if (!message.success) { unreadable++; continue }
        const trigger = webhookTrigger(message.data, page.me)
        if (!trigger) continue
        await ctx.receive(trigger)
        const conversation = { label: { zh: '对话', en: 'Conversation' }, value: message.data.conversation }
        for (const ref of trigger.threads) ctx.threads.describe(threadKey(ref), { facts: { conversation } })
      }
      ctx.state.set(KV.webhook.cursor, page.next)
      return { read: page.items.length, unreadable }
    }
    ```


    - **Setup and the switch.** Wait for `ctx.ready`, and read nothing while `ctx.configured()` is false. Read `ctx.config(kind)` on every round: it is undefined while the Source is off.
    - **Cursors.** Store read positions in `ctx.state` using typed entries in `KV` (`src/kv.ts`), such as `webhook: {cursor: record<number>('webhook-cursor')}`. `tests/store.spec.ts` requires key literals to appear only in that registry. External packages can define keys with exported `record` and `family`. Setting `undefined` removes a record; `ctx.atomic` groups writes in a transaction.

      Advance a cursor only after every covered Trigger has been stored. An interrupted round leaves the cursor unchanged; stable ids make replay safe. For timestamp cursors, `cursorAt(started)` from `src/sources/poll.ts` subtracts five seconds to allow for late remote writes.
    - **Thread metadata.** After receiving a Trigger, call `ctx.threads.describe(key, {label, url, facts})` to populate its display information. `ctx.threads.patch(key, meta)` delegates to `threads.patchMeta`, setting Source-owned keys or removing them when the value is `undefined`. Both ignore absent Threads, including those of filtered Triggers.
    - **Per-item errors.** Count malformed items and continue rather than failing the entire batch. `validItems(item, list, tally)` in `src/infra/commands.ts` supports this pattern. For independently polled Threads, use `pollThreads` from `src/sources/poll.ts`; it records each failure as `pollError`. Its `polled` predicate keeps live subscriptions and a two-week window after the later of the last Trigger and Ticket end.
    - **Health.** Write only declared entries through `ctx.report`. `health(state, code, values)` provides fixed messages such as `setupPending`, `sourceOff` and `pollFailures`. Count consecutive failures before reporting `error`.
    - **Abort.** Pass the signal to `ctx.commands.withSignal(signal)` and to `delay`, and check it between items, so a stop or a restart ends the loop at once.

    This example does not send Source-originated notices. A Source that needs them calls `ctx.notify`; Outbox delivers them like other replies.

    It also does not bind Threads to Ticket completion. Use `ctx.tickets.bindThread` and `threadClosed` only when a Source's Thread lifecycle determines whether work is done, as GitHub pull requests do. The Engine still decides when the Ticket ends.

    ### 10. Register the plugin

    Build the module as its own entry in `tsdown.config.ts`:

    ```ts
    entry: { index: 'src/index.ts', 'github-watch': 'src/sources/github.ts', 'feishu-watch': 'src/sources/feishu.ts',
      'webhook-watch': 'src/sources/webhook.ts' },
    ```

    Then export the built file in `package.json` by adding `"./webhook-watch": "./lib/webhook-watch.js"` to `exports`, and add the plugin to the `insert` list of the bundle's `cordis.patch.yml`, disabled, as Feishu is:

    ```yaml
    - id: dsh-superagent-webhook
      name: dsh-superagent/webhook-watch
      disabled: true
    ```

    Enable the plugin in a profile's `cordis.patch.yml` with `id: dsh-superagent-webhook` and `disabled: false`. Build and restart. The new Source appears in Sources settings and under **Eval → On each Source**.

    Add its module and spec to the module and test tables in [007 Architecture](proto-007-architecture.md). `tests/docs.spec.ts` checks that both indexes are complete.

    ### 11. Tests

    Create `tests/webhook.spec.ts` using the pattern in `tests/sources.spec.ts`: a real Store in a temporary directory, a `SourceRegistry` with `HealthBoard`, an Engine with stand-in ports, and a `SourceContext` from `sourceContextOf`.

    Mock CLI calls by spying on `Commands.run`. Also spy on `withSignal`, because it normally returns a new `Commands` instance.

    ```ts
    function host(overrides: Partial<SourceContextDeps> = {}) {
      const store = new Store(join(mkdtempSync(join(tmpdir(), 'webhook-')), 'db.sqlite'))
      store.saveSettings(SettingsSchema.parse({
        sources: { webhook: { enabled: true, config: { conversations: [{ id: 'ops', listen: 'mentions' }] } } },
        policy: { sources: { webhook: { allowed: [{ id: 'alice' }] } } },
      }))
      // The registry claims the kind's health entries on the board, where the test reads them.
      const board = new HealthBoard()
      const sources = new SourceRegistry(() => store.settings(), board)
      sources.register(webhookKind)
      const engine = new Engine({ repo: store, scope: sources, policy: () => settingsPolicy(store.settings(), sources),
        assessor: { request: vi.fn() }, executor: { start: vi.fn(), deliver: vi.fn(), stop: vi.fn() },
        outbox: { send: vi.fn(), status: () => undefined, withdraw: vi.fn() } })
      const commands = new Commands(() => testDeployment(), {}, new AbortController().signal)
      const receive = vi.fn((trigger: Trigger) => { const result = engine.receive(trigger); engine.evaluateDue(); return result })
      // The Source's own notices go to the outbox, which keeps their receipts and sends nothing here.
      const deps: SourceContextDeps = { store, engine, outbox: { send: message => { store.receipts.add(message) } }, commands, sources,
        deployment: testDeployment(), ready: Promise.resolve(), configured: () => true, receive, ...overrides }
      return { ...deps, store, engine, commands, receive, health: board.all, ctx: sourceContextOf(deps, webhookKind) }
    }
    /** The queue as `hookq` answers it. */
    function queue(h: ReturnType<typeof host>, answer: () => unknown) {
      vi.spyOn(h.commands, 'withSignal').mockReturnValue(h.commands)
      return vi.spyOn(h.commands, 'run').mockImplementation(async () => JSON.stringify(answer()))
    }

    it('takes in what the queue holds after the cursor, once, and moves the cursor after it', async () => {
      const h = host()
      queue(h, () => ({ me: 'agent', items: [message('m1'), { id: 'broken' }, message('m2', { conversation: 'elsewhere' })], next: 3 }))
      expect(await pollQueue(h.ctx, new AbortController().signal)).toEqual({ read: 3, unreadable: 1 })
      expect(await h.receive.mock.results[0]!.value).toBe('stored')
      expect(await h.receive.mock.results[1]!.value).toBe('filtered')
      expect(h.store.get(KV.webhook.cursor)).toBe(3)
      // An allowed sender who mentions the agent makes an explicit request.
      expect(h.engine.candidate(key('webhook', 'm1'))?.strength).toBe('explicit')
      await pollQueue(h.ctx, new AbortController().signal)
      expect(await h.receive.mock.results[2]!.value).toBe('duplicate')
      h.store.close()
    })
    ```

    `message(id, fields)` builds a queue item. Cover at least:

    - the translation: a stable id, the Thread, the actor, `human` and `addressed`, and no Trigger for the agent's own message;
    - a round: Triggers stored once, one outside the scope `filtered`, the cursor moved after them, an allowed sender's mention evaluated as `explicit`;
    - a round cut short, which keeps its cursor;
    - `send`: `sent`, `gone` for a 410 only, and a throw for any other failure, with the CLI's error built by `commandError`;
    - the Thread context: the messages of a page and the cursor of the next, as `tests/registry.spec.ts` reads a kind's Threads through `SourceRegistry.threadContext`;
    - the plugin: `apply` registers the kind and reports `setupPending` while setup waits, its disposers unregister the kind, and a failure that escapes the loop becomes the Source's health instead of an unhandled rejection;
    - the Source's part of the Policy and its admission rows, as `tests/registry.spec.ts` and `tests/policy.spec.ts` check the Feishu Source's.

!!! quote "译文（中文）"

    ### 1. 将事件映射到领域

    写代码前先选定稳定映射。Trigger id、Thread id 与 Channel 地址会持久化，事后改动可能使既有记录脱节。

    | 概念 | 对 webhook 队列 |
    | --- | --- |
    | Trigger | 一条消息 |
    | Thread | 同一对话中某条消息及其回复：`<conversation>:<首条消息的 id>` |
    | Actor | 队列上发送者 id，与允许名单中的命名一致 |
    | Channel 地址 | 对话以及回复所挂靠的消息 |
    | 接收范围 | 一个对话，在准入表中有独立一行 |

    ### 2. 配置：schema、fields、facts 与 health

    模块导入后续步骤所用：

    ```ts
    import type { Context } from '@deepseek-ai/cordis'
    import { setTimeout as delay } from 'node:timers/promises'
    import { z } from 'zod'
    import { key, threadKey, type OutboundMessage, type Trigger } from '../core/index.ts'
    import { CliError } from '../infra/commands.ts'
    import { KV } from '../kv.ts'
    import { health, sourceContext, type SourceContext } from './host.ts'
    import type { SourceKind } from './kind.ts'
    ```

    ```ts
    const ConfigSchema = z.object({
      conversations: z.array(z.object({
        id: z.string().regex(/^[a-z0-9-]+$/),
        listen: z.enum(['mentions', 'all']),
      }).strict()).default([]),
      pollSeconds: z.number().int().min(10).max(3600).default(30),
    }).strict()
    export type WebhookConfig = z.infer<typeof ConfigSchema>

    export const webhookKind: SourceKind<WebhookConfig> = {
      kind: 'webhook',
      title: { zh: 'Webhook 队列', en: 'Webhook queue' },
      description: { zh: '接收 webhook 队列里下列对话的消息。', en: 'Receives the messages of these conversations from the webhook queue.' },
      schema: ConfigSchema,
      fields: [
        { key: 'conversations', type: 'rows', label: { zh: '接收的对话', en: 'Conversations' }, columns: [
          { key: 'id', type: 'text', label: { zh: '对话', en: 'Conversation' }, pattern: '[a-z0-9-]+' },
          { key: 'listen', type: 'select', label: { zh: '接收', en: 'Receive' }, options: [
            { value: 'mentions', label: { zh: '仅 @', en: '@ only' } }, { value: 'all', label: { zh: '全部消息', en: 'All' } }] },
        ] },
        { key: 'pollSeconds', type: 'number', advanced: true, min: 10, max: 3600,
          label: { zh: '轮询间隔 / 秒', en: 'Polling interval / seconds' } },
      ],
      health: ['webhook'],
      // 后续步骤补充 find、scope、channel、threadContext、policy 与 environment。
    }
    ```

    - `kind` 是稳定的 Source id，出现在 Trigger 的 `source`、Thread 键前缀、`source:webhook` 设置块与 Source Policy 中。
    - `schema` 校验已存配置并提供默认值。操作者保存实例前，registry 会按默认值创建已启用实例。默认应不接收任何内容；本例默认无对话。
    - `fields` 生成 Source 设置表单，无需自定义面板代码。`rows` 字段以首列为主键编辑条目；`advanced` 隐藏较少用的设置。若 Source 能把 id 解析为显示名，实现 `refreshLabels`，并将名称存于 `KV.sources.labels(kind)`。带 `choose` 与 `refresh` 的列即可提供这些名称。
    - 可选 `facts(deployment)` 展示 setup 推导的值，例如 CLI 背后的账号。Setup 目前只检查 `SETUP_FIELDS` 列出的 GitHub 与 Feishu 字段，故本例不提供 facts。
    - `health` 声明 Source 的健康项，初始为 `starting`。注册时认领每个名称，并与 `runtime`、`reply`、`worker` 或 effect 种类等项冲突则拒绝。使用 kind 名，并为 Policy 同步保留 `sync:<id>`。`error` 会使状态脚本以 2 退出、每小时检查通知操作者，因此仅在可能需要人工介入时上报。

    ### 3. 将消息译为 Trigger

    转换前先校验 CLI 响应：

    ```ts
    /** `hookq pull` 与 `hookq thread` 打印的单条消息。 */
    const MessageSchema = z.looseObject({
      id: z.string().min(1), conversation: z.string().min(1), thread: z.string().optional(),
      sender: z.looseObject({ id: z.string(), bot: z.boolean().default(false) }), mentions_me: z.boolean().default(false),
      text: z.string(), at: z.string(), url: z.string().optional(),
    })
    type QueueMessage = z.infer<typeof MessageSchema>
    /** `hookq pull` 的应答（items 逐条读取）以及 `hookq thread` 的应答。 */
    const PageSchema = z.looseObject({ me: z.string(), items: z.array(z.unknown()), next: z.number().int() })
    const ThreadPageSchema = z.looseObject({ items: z.array(MessageSchema), next: z.string().optional() })
    ```

    ```ts
    /** 单条队列消息的 Trigger；智能体自己的消息不产生 Trigger。 */
    export function webhookTrigger(message: QueueMessage, me: string): Trigger | undefined {
      if (message.sender.id === me) return undefined
      const thread = `${message.conversation}:${message.thread ?? message.id}`
      return {
        id: key('webhook', message.id),
        source: 'webhook',
        threads: [{ source: 'webhook', id: thread }],
        actor: { source: 'webhook', id: message.sender.id },
        human: !message.sender.bot,
        ...(message.mentions_me ? { addressed: 'mention' as const } : {}),
        at: message.at,
        text: message.text,
        channel: { source: 'webhook', thread: `webhook:${thread}`,
          address: { conversation: message.conversation, message: message.id, ...(message.url ? { url: message.url } : {}) } },
        ...(message.url ? { url: message.url } : {}),
        meta: { conversation: message.conversation, messageId: message.id, root: message.thread ?? message.id },
      }
    }
    ```

    - **稳定 id。** 通过 `key` 从队列 id 派生 Trigger id。对已存 id，`Engine.receive` 返回 `duplicate`，重复 accept 只创建一个 Ticket，因此重读消息安全。若要把编辑当作新事件，在 id 中加入编辑版本，如 GitHub 对评论使用 `updated_at`。
    - **Thread。** 第一个 Thread 为主 Thread 并拥有 Eval。额外 Thread 增加 Correlation 上下文。Source 自选 id；完整键为 `webhook:<id>`。
    - **发送者。** `actor` 使用 Source 原生 id。bot 与 CI 设 `human: false`。名单中的 bot 只能成为 inferred 候选。空 actor id（如 CI）使用未列入、未 @ 发送者的准入单元。仅当消息确实 @ 智能体时将 `addressed` 设为 `mention` 或 `direct`，使允许名单内的人消息具有 explicit 强度。切勿为智能体自己的消息产生 Trigger。
    - **回复目的地与元数据。** `channel` 标识回复去向；`url` 链到源消息。`channel.thread` 将消息分组以有序投递：同一 Ticket 的后续消息排在前序之后，直到前一条失败三次；其他 Ticket 不等待。Source 专有字段放在 `meta`，如接收对话与 Thread 根。`meta` 与 `text` 均为不可信数据，不是授权依据。

    Agent 可通过 `orchestrator_read trigger/<id>` 与 `thread/<key>` 读取新 kind，暴露消息文本、处置与相邻 Trigger。新 kind 不会自动获得内置 skill、原始服务记录资源或 PR 绑定。

    全文搜索无需额外代码。当用户使用 Source 专有引用（消息 id、URL 或 `#12`）时实现 `find(query)`，返回这些引用所识别的 Thread 键与搜索词：

    ```ts
      find: query => ({ threads: [], terms: [...new Set(query.match(/\bm[0-9]+\b/g) ?? [])] }),
    ```

    例如查询中的队列消息 id `m42` 会搜索对应 Trigger。

    ### 4. Source 接收什么：scope

    ```ts
      scope({ meta }, config) {
        const conversation = config.conversations.find(item => item.id === meta.conversation)
        if (!conversation) return { listened: false, reason: 'conversation_not_listed' }
        return { listened: true, observed: conversation.listen === 'all', row: conversation.id }
      },
    ```

    Engine 经 `SourceRegistry` 对每条入站 Trigger 调用 `scope`。范围外的消息以 filtered 存储并附原因，不创建 Thread。应把消息交给 `receive`，而不是在轮询循环中静默丢弃，以便面板解释过滤。

    `observed: true` 表示 Source 在该范围内接收未 @ 的消息，从而允许准入表对其决策。`row` 标识对应准入行。

    重校验用 Mandate 所存 metadata 调用 `scope`。应仅依赖 `meta` 与配置。恢复要求 `listened`，不要求 `observed`。无 `scope` 时，kind 接收其产生的全部 Trigger 但不 observe 任何一条。

    ### 5. 回复：Channel

    ```ts
      channel: {
        allowed: (address, config) => config.conversations.some(item => item.id === address.conversation),
        send: sendWebhook,
        link: address => ({ url: address.url ?? '',
          label: { zh: `Webhook 对话 ${address.conversation}`, en: `Webhook conversation ${address.conversation}` } }),
      },
    ```

    ```ts
    export async function sendWebhook(host: Pick<SourceContext, 'commands'>, message: OutboundMessage): Promise<'sent' | 'gone'> {
      const { conversation, message: to } = message.channel.address
      if (!conversation || !to) throw new Error('Missing webhook reply target')
      try {
        await host.commands.run('hookq', ['reply', '--conversation', conversation, '--to', to, '--key', message.id,
          '--text', message.text])
        return 'sent'
      } catch (error) {
        if (error instanceof CliError && error.code === 410) return 'gone'
        throw error
      }
    }
    ```

    - **幂等发送。** Outbox 调用 `SourceRegistry.send`，进而调用 `channel.send`。Source 插件卸载期间消息等待。发送可能在远端已收到后仍失败，重试不得重复消息。用 `message.id` 作幂等键：`hookq` 支持 `--key`；GitHub 存隐藏评论标记；Feishu 接受幂等键。
    - **永久失败与可重试失败。** 成功返回 `sent`；仅当投递永久不可能（如本例 410）时返回 `gone`。gone 消息保留为 dropped 且不再入队；出现在 `GET sources` 与 Source 页。其他失败应抛出，Outbox 5 秒后重试，延迟加倍至 300 秒。`CliError.code` 为 CLI 输出报告的错误码。
    - **允许的目的地。** `allowed(address, config)` 只检查已存地址与当前配置。地址中放入一切必要事实；例如 Feishu 记录聊天是否 direct。不再监听的 destination 被丢弃；Source 禁用则消息等待。无 `allowed` 时，Source 启用则允许全部地址。
    - **链接。** `link(address, links)` 返回面板经 `SourceRegistry.link` 展示的 URL 与标签。

    ### 6. 评估用的 Thread 上下文

    ```ts
      threadContext: {
        messageId: trigger => String(trigger.meta.messageId ?? ''),
        // 队列 thread 是扁平的：根与回复所针对的消息都在第一页，与 GitHub 类似。
        read: async (commands, trigger, _part, cursor) => {
          const page = ThreadPageSchema.parse(JSON.parse(await commands.run('hookq', ['thread', '--conversation',
            String(trigger.meta.conversation), '--root', String(trigger.meta.root), '--json', ...(cursor ? ['--after', cursor] : [])])))
          const messages = page.items.map(item => ({ id: item.id, from: item.sender.id, at: item.at, text: item.text,
            ...(item.sender.bot ? { bot: true as const } : {}), ...(item.url ? { url: item.url } : {}) }))
          return { messages, ...(page.next ? { next: page.next } : {}), body: { messages } }
        },
      },
    ```

    主动评估需要 Trigger 周围的对话。`orchestrator_eval` 读初始窗口；`orchestrator_eval_read` 扩展页、根或父消息。实现可选 `threadContext` 接口以提供这些读取：

    - `read(commands, trigger, part, cursor, config)` 返回 `ContextPart`。其 `messages` 使用 `ContextMessage`：`{id, from, at, text}` 及适用的 `bot`、`deleted`、`reply_to`、`url`、`path`、`line`。最后一页省略 `next`。`body` 是展示给评估 Session 的内容，此处与 messages 相同。可选 `status` 可描述特殊结果，如 Feishu 的 `chat_neighbours`。`part` 为 `thread`、`root` 或 `parent`。
    - `messageId(trigger)` 在其中定位 Trigger 自身消息。
    - `images(text)` 与 `image(…)` 用于消息带图的 kind，如 Feishu；本例省略。

    无 `threadContext` 的 kind 不给评估提供 Thread 上下文；Trigger 自身文本仍可读。

    若消息回复更早消息，添加 `repliesTo(trigger)`，返回对应 Trigger id，使后续通知与 worker Input 在已存储时展示被回复消息。例如 `hookq` 提供 `reply_to` 且翻译存为 `meta.replyTo`：

    ```ts
      repliesTo: trigger => typeof trigger.meta.replyTo === 'string' ? key('webhook', trigger.meta.replyTo) : undefined,
    ```

    ### 7. 谁可发起：Source 在 Policy 中的部分

    ```ts
      policy: {
        schema: z.object({ allowed: z.array(z.object({ id: z.string().min(1), name: z.string().trim().max(100).optional() }).strict())
          .default([]) }).strict(),
        fields: [{ key: 'allowed', type: 'rows', label: { zh: '能发起工作的发送者', en: 'Senders who may originate work' }, columns: [
          { key: 'id', type: 'text', label: { zh: '发送者', en: 'Sender' } },
          { key: 'name', type: 'text', label: { zh: '名字', en: 'Name' }, optional: true }] }],
        explicit: { zh: '名单里的人在消息里 @ 智能体。', en: 'Someone on the list mentions the agent in a message.' },
        rows: stored => ConfigSchema.parse(stored).conversations.map(conversation => {
          const all = conversation.listen === 'all'
          return { id: conversation.id, label: conversation.id, note: all ? { zh: '全部消息', en: 'All' } : { zh: '仅 @', en: '@ only' },
            cells: { listedUnaddressed: all, unlistedAddressed: true, unlistedUnaddressed: all } }
        }),
      },
    ```

    - `schema` 校验 Source Policy，含 `allowed` 发送者 id。id 比较不区分大小写。准入表单独校验并在各 kind 间共享。此 Policy 部分存于 `policy.sources.webhook` 并计入 Policy 版本。
    - `fields` 在 **Eval → On each Source** 下生成表单，以 `allowed` 开头。`explicit` 说明如何 @ 智能体。可选 `unnamed: {person, bot}` 为 opaque id（如 Feishu 的 `ou_…`）提供显示标签；本例省略，因队列 id 可读。
    - `rows(config)` 列出准入范围及可用单元。每行 id 须与 `scope` 返回的 `row` 一致。未 @ 消息单元仅在 Source observe 该范围时可用。无 `rows` 的 kind 使用默认单元。
    - 可选 `bots` 列出可 inferred 候选的 bot，条目形状同 `allowed`。无则不接受任何 bot。
    - 可选 `sync: {label, ready(part), run(host, part, signal)}` 在启动与 **Sync now** 时刷新成员。Feishu 用聊天成员。失败出现在 `sync:<id>` 下，一分钟后重试，加倍至一小时直至成功。

    ### 8. Executor 用变量

    ```ts
      environment: {
        variables: [{ key: 'DSH_SUPERAGENT_WEBHOOK_CONVERSATION', description: {
          zh: '接下工单的 webhook 对话；工单不是从这个来源来的时为空',
          en: 'The webhook conversation that started the ticket; empty when the ticket did not start on this Source' } }],
        values: ({ trigger }) => ({
          DSH_SUPERAGENT_WEBHOOK_CONVERSATION: trigger?.source === 'webhook' ? String(trigger.meta.conversation ?? '') : '' }),
      },
    ```

    执行设置中，即使 Source 禁用也会在 prepare 命令旁展示已声明的 `variables`。启用时 `values` 用 deployment、配置与 originating Trigger 提供 Ticket 专有值。

    prepare 命令读环境变量；worker 在指令中读其值与说明。使用 `DSH_SUPERAGENT_<KIND>_…` 前缀；来自其他 Source 的 Ticket 返回空值；切勿包含凭据。

    ### 9. 插件入口与循环

    ```ts
    export const name = 'dsh-superagent-webhook'
    export const inject = ['orchestrator']
    export function apply(ctx: Context): void {
      ctx.effect(() => ctx.orchestrator.sources.register(webhookKind), 'webhook: source kind')
      ctx.effect(() => {
        const source = sourceContext(ctx.orchestrator, webhookKind)
        const controller = new AbortController()
        // DSH 在未处理 rejection 时会结束进程，故逃出循环的失败会成为 Source 的 health。
        const running = watch(source, controller.signal)
          .catch((error: unknown) => { source.report('webhook', { state: 'error', detail: String(error) }) })
        return async () => { controller.abort(); await running }
      })
    }
    ```

    插件注入 `orchestrator`。第一个 effect 注册 kind，dispose 时注销。第二个用 `SourceContext` 运行轮询循环。Dispose 中止循环并等待完成，包括应用停止或重启时。

    ```ts
    async function watch(ctx: SourceContext, signal: AbortSignal): Promise<void> {
      await ctx.ready
      // Setup 解析账号后会重启 dsh-superagent；此前 Source 不读取。
      if (!ctx.configured()) { ctx.report('webhook', health('paused', 'setupPending')); return }
      let failures = 0
      while (!signal.aborted) {
        const config = ctx.config<WebhookConfig>('webhook')
        if (!config) ctx.report('webhook', health('paused', 'sourceOff'))
        else {
          const at = new Date().toISOString()
          try {
            const { read, unreadable } = await pollQueue(ctx, signal)
            failures = 0
            ctx.report('webhook', { state: 'ready', detail: `${read} messages, ${unreadable} unreadable`, at })
          } catch (error) {
            failures += 1
            // 单次网络失败下一轮重试；连续三次需人工。
            if (!signal.aborted && failures >= 3) {
              ctx.report('webhook', health('error', 'pollFailures', { count: String(failures), error: String(error) }))
            }
          }
        }
        await delay((config?.pollSeconds ?? 30) * 1000, undefined, { signal }).catch(() => {})
      }
    }

    /** 在游标之后读队列，逐条 intake，再将游标移过它们。 */
    export async function pollQueue(ctx: SourceContext, signal: AbortSignal): Promise<{ read: number; unreadable: number }> {
      const after = ctx.state.get(KV.webhook.cursor) ?? 0
      const output = await ctx.commands.withSignal(signal).run('hookq', ['pull', '--after', String(after), '--json'])
      const page = PageSchema.parse(JSON.parse(output))
      let unreadable = 0
      for (const item of page.items) {
        if (signal.aborted) throw new Error('The webhook Source stopped during the read')
        const message = MessageSchema.safeParse(item)
        if (!message.success) { unreadable++; continue }
        const trigger = webhookTrigger(message.data, page.me)
        if (!trigger) continue
        await ctx.receive(trigger)
        const conversation = { label: { zh: '对话', en: 'Conversation' }, value: message.data.conversation }
        for (const ref of trigger.threads) ctx.threads.describe(threadKey(ref), { facts: { conversation } })
      }
      ctx.state.set(KV.webhook.cursor, page.next)
      return { read: page.items.length, unreadable }
    }
    ```


    - **Setup 与开关。** 等待 `ctx.ready`；`ctx.configured()` 为 false 时不读。每轮读 `ctx.config(kind)`：Source 关闭时为 undefined。
    - **游标。** 用 `KV`（`src/kv.ts`）中的类型化条目在 `ctx.state` 存读取位置，例如 `webhook: {cursor: record<number>('webhook-cursor')}`。`tests/store.spec.ts` 要求键字面量仅出现在该 registry。外部包可用导出的 `record` 与 `family` 定义键。设 `undefined` 删除记录；`ctx.atomic` 将写入分组为事务。

      仅在所涉 Trigger 均已存储后推进游标。中断的一轮游标不变；稳定 id 使重放安全。时间戳游标可用 `src/sources/poll.ts` 的 `cursorAt(started)` 减五秒以容纳远端迟写。
    - **Thread 元数据。** 接收 Trigger 后调用 `ctx.threads.describe(key, {label, url, facts})` 填充展示信息。`ctx.threads.patch(key, meta)` 委托 `threads.patchMeta`，设置 Source 拥有的键或在值为 `undefined` 时删除。二者均忽略不存在的 Thread，含 filtered Trigger 的 Thread。
    - **逐条错误。** 统计畸形条目并继续，而非整批失败。`src/infra/commands.ts` 的 `validItems(item, list, tally)` 支持此模式。独立轮询的 Thread 用 `src/sources/poll.ts` 的 `pollThreads`；每次失败记为 `pollError`。其 `polled` 谓词保留 live 订阅及最后 Trigger 与 Ticket 结束中较晚者之后两周的窗口。
    - **Health。** 仅通过 `ctx.report` 写入已声明条目。`health(state, code, values)` 提供 `setupPending`、`sourceOff`、`pollFailures` 等固定消息。连续失败达阈值再报 `error`。
    - **Abort。** 将 signal 传给 `ctx.commands.withSignal(signal)` 与 `delay`，并在条目间检查，使停止或重启立即结束循环。

    本例不发送 Source 发起的通知。需要的 Source 调用 `ctx.notify`；Outbox 与其他回复一样投递。

    本例也不把 Thread 绑定到 Ticket 完成。仅当 Source 的 Thread 生命周期决定工作是否完成时使用 `ctx.tickets.bindThread` 与 `threadClosed`，如 GitHub PR。Ticket 何时结束仍由 Engine 决定。

    ### 10. 注册插件

    在 `tsdown.config.ts` 中将模块建为独立 entry：

    ```ts
    entry: { index: 'src/index.ts', 'github-watch': 'src/sources/github.ts', 'feishu-watch': 'src/sources/feishu.ts',
      'webhook-watch': 'src/sources/webhook.ts' },
    ```

    在 `package.json` 的 `exports` 增加 `"./webhook-watch": "./lib/webhook-watch.js"`，并在 bundle 的 `cordis.patch.yml` 的 `insert` 列表加入插件，默认 disabled，与 Feishu 相同：

    ```yaml
    - id: dsh-superagent-webhook
      name: dsh-superagent/webhook-watch
      disabled: true
    ```

    在 profile 的 `cordis.patch.yml` 中以 `id: dsh-superagent-webhook`、`disabled: false` 启用插件。构建并重启。新 Source 出现在 Sources 设置与 **Eval → On each Source** 下。

    将其模块与 spec 列入 [007 架构](proto-007-architecture.md) 的模块表与测试表。`tests/docs.spec.ts` 检查两份索引完整。

    ### 11. 测试

    按 `tests/sources.spec.ts` 模式创建 `tests/webhook.spec.ts`：临时目录中的真实 Store、带 `HealthBoard` 的 `SourceRegistry`、替身端口的 Engine，以及来自 `sourceContextOf` 的 `SourceContext`。

    通过 spy `Commands.run` 模拟 CLI。同时 spy `withSignal`，因其通常返回新的 `Commands` 实例。

    ```ts
    function host(overrides: Partial<SourceContextDeps> = {}) {
      const store = new Store(join(mkdtempSync(join(tmpdir(), 'webhook-')), 'db.sqlite'))
      store.saveSettings(SettingsSchema.parse({
        sources: { webhook: { enabled: true, config: { conversations: [{ id: 'ops', listen: 'mentions' }] } } },
        policy: { sources: { webhook: { allowed: [{ id: 'alice' }] } } },
      }))
      // registry 在 board 上认领 kind 的 health 项，测试从 board 读取。
      const board = new HealthBoard()
      const sources = new SourceRegistry(() => store.settings(), board)
      sources.register(webhookKind)
      const engine = new Engine({ repo: store, scope: sources, policy: () => settingsPolicy(store.settings(), sources),
        assessor: { request: vi.fn() }, executor: { start: vi.fn(), deliver: vi.fn(), stop: vi.fn() },
        outbox: { send: vi.fn(), status: () => undefined, withdraw: vi.fn() } })
      const commands = new Commands(() => testDeployment(), {}, new AbortController().signal)
      const receive = vi.fn((trigger: Trigger) => { const result = engine.receive(trigger); engine.evaluateDue(); return result })
      // Source 自有通知进 outbox，此处保留 receipt 但不真正发送。
      const deps: SourceContextDeps = { store, engine, outbox: { send: message => { store.receipts.add(message) } }, commands, sources,
        deployment: testDeployment(), ready: Promise.resolve(), configured: () => true, receive, ...overrides }
      return { ...deps, store, engine, commands, receive, health: board.all, ctx: sourceContextOf(deps, webhookKind) }
    }
    /** `hookq` 的队列应答。 */
    function queue(h: ReturnType<typeof host>, answer: () => unknown) {
      vi.spyOn(h.commands, 'withSignal').mockReturnValue(h.commands)
      return vi.spyOn(h.commands, 'run').mockImplementation(async () => JSON.stringify(answer()))
    }

    it('takes in what the queue holds after the cursor, once, and moves the cursor after it', async () => {
      const h = host()
      queue(h, () => ({ me: 'agent', items: [message('m1'), { id: 'broken' }, message('m2', { conversation: 'elsewhere' })], next: 3 }))
      expect(await pollQueue(h.ctx, new AbortController().signal)).toEqual({ read: 3, unreadable: 1 })
      expect(await h.receive.mock.results[0]!.value).toBe('stored')
      expect(await h.receive.mock.results[1]!.value).toBe('filtered')
      expect(h.store.get(KV.webhook.cursor)).toBe(3)
      // 允许的发送者 @ 智能体构成 explicit 请求。
      expect(h.engine.candidate(key('webhook', 'm1'))?.strength).toBe('explicit')
      await pollQueue(h.ctx, new AbortController().signal)
      expect(await h.receive.mock.results[2]!.value).toBe('duplicate')
      h.store.close()
    })
    ```

    `message(id, fields)` 构造队列 item。至少覆盖：

    - 翻译：稳定 id、Thread、actor、`human` 与 `addressed`，且智能体自己的消息无 Trigger；
    - 一轮：Trigger 只存一次、一条范围外为 `filtered`、游标在其后移动、允许发送者 @ 评估为 `explicit`；
    - 被截断的一轮保持游标；
    - `send`：仅 410 为 `gone`，其他失败抛出，CLI 错误由 `commandError` 构造；
    - Thread 上下文：一页 messages 与下一页 cursor，如 `tests/registry.spec.ts` 经 `SourceRegistry.threadContext` 读 kind 的 Thread；
    - 插件：`apply` 注册 kind、setup 等待时报 `setupPending`、disposer 注销 kind、逃出循环的失败成为 Source health 而非未处理 rejection；
    - Source 的 Policy 部分与准入行，如 `tests/registry.spec.ts` 与 `tests/policy.spec.ts` 对 Feishu Source 的检查。

## Checklist

!!! note "原文（English）"

    ### Checklist

    1. Map Triggers, Threads, actors, Channel addresses and receiving scopes.
    2. Write the kind: `schema`, `fields`, `health`, and `facts` if setup derives any.
    3. Translate events into Triggers with stable ids, and drop the agent's own.
    4. Decide what the Source receives in `scope`, from `meta` alone.
    5. Give it a Channel whose `send` posts each message once and answers `gone` only when the conversation can never receive it.
    6. Give assessments its Thread context, if it can read a Thread.
    7. Give it a part of the Policy with `allowed` and the admission rows.
    8. Register its execution environment variables.
    9. Write the plugin entry and a loop that waits for setup, follows the switch, moves cursors after `receive`, keeps errors per item, reports health and stops on abort; name the cursors in `KV`.
    10. Add the build entry, the export and the `cordis.patch.yml` insert, and list the module and its spec in the tables of [007 Architecture](proto-007-architecture.md).
    11. Test it as `tests/sources.spec.ts` tests the other Sources.

!!! quote "译文（中文）"

    ### 检查清单

    1. 映射 Trigger、Thread、actor、Channel 地址与接收范围。
    2. 编写 kind：`schema`、`fields`、`health`，若 setup 有推导则加 `facts`。
    3. 用稳定 id 将事件译为 Trigger，并丢弃智能体自己的消息。
    4. 在 `scope` 中仅凭 `meta` 决定 Source 接收什么。
    5. 提供 Channel：`send` 每条消息只发一次，仅当对话永不可收时返回 `gone`。
    6. 若能读 Thread，为评估提供 Thread 上下文。
    7. 提供 Policy 部分：含 `allowed` 与准入行。
    8. 注册执行环境变量。
    9. 编写插件入口与循环：等待 setup、跟随开关、在 `receive` 后移动游标、逐条保留错误、上报 health、abort 时停止；在 `KV` 中命名游标。
    10. 添加构建 entry、export 与 `cordis.patch.yml` insert，并在 [007 架构](proto-007-architecture.md) 的表中列出模块与其 spec。
    11. 按 `tests/sources.spec.ts` 测试其他 Source 的方式测试。
