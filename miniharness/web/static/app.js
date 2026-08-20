"use strict";

// MiniHarness web 前端：vanilla SPA（教学简化：无 React、无 slot 组合系统）。
// 数据面 = 上游协议：POST /api/<method>（client-request）+ GET /api/events.mux
// （server-request 帧流）+ POST /api/respond（client-response）。

const $ = (id) => document.getElementById(id);
const muted = (text) => el("p", "muted", text);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---------- RPC ----------

async function rpc(method, payload) {
  const res = await fetch("/api/" + method, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      type: "client-request",
      rpcId: crypto.randomUUID(),
      method: method,
      payload: payload || {},
    }),
  });
  if (res.status !== 200) throw new Error(method + " → HTTP " + res.status);
  const message = await res.json();
  if (!message.result.ok) {
    const error = message.result.error || {};
    throw new Error((error.code || "error") + ": " + (error.message || ""));
  }
  return message.result.value;
}

async function respond(rpcId, value) {
  await fetch("/api/respond", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      type: "client-response",
      rpcId: rpcId,
      result: { ok: true, value: value },
    }),
  });
}

// ---------- 状态 ----------

const state = {
  describe: null,
  sessions: [],
  current: null,      // sessionId
  events: [],         // 当前会话的 event 折叠视图（按 seq 升序）
  seenSeqs: new Set(),
  approvals: new Map(), // rpcId -> card
  queue: [],
  jobs: [],
  mux: null,
};

// ---------- 会话列表 ----------

async function refreshSessions(selectId) {
  const value = await rpc("session.list", {});
  state.sessions = value.items || [];
  const list = $("session-list");
  list.textContent = "";
  for (const item of state.sessions) {
    const li = el("li", "session-item" + (item.sessionId === state.current ? " active" : ""));
    const head = el("div", "sid", item.sessionId);
    const meta = el("div", "meta",
      (item.running ? "running" : "idle") +
      (item.blank ? " · blank" : "") +
      (item.cwd ? " · " + item.cwd : ""));
    li.append(head, meta);
    li.addEventListener("click", () => selectSession(item.sessionId));
    list.append(li);
  }
  if (selectId && state.sessions.some((s) => s.sessionId === selectId)) {
    selectSession(selectId);
  }
}

// ---------- mux 流 ----------

function openMux() {
  if (state.mux) state.mux.close();
  const es = new EventSource("/api/events.mux");
  es.addEventListener("message", (ev) => {
    let message;
    try { message = JSON.parse(ev.data); } catch { return; }
    if (message.type !== "server-request") return;
    handleFrame(message.method, message.payload);
  });
  state.mux = es;
}

function handleFrame(method, payload) {
  if (method === "session/subscribed") {
    if (payload.sessionId === state.current) renderEmpty();
    return;
  }
  if (method === "session/event") {
    if (payload.sessionId !== state.current) return;
    const event = payload.event;
    if (state.seenSeqs.has(event.seq)) return;
    state.seenSeqs.add(event.seq);
    state.events.push(event);
    state.events.sort((a, b) => a.seq - b.seq);
    renderTrajectory();
    return;
  }
  if (method === "session/queue") {
    if (payload.sessionId === state.current) { state.queue = payload.items || []; renderQueue(); }
    return;
  }
  if (method === "session/jobs") {
    if (payload.sessionId === state.current) { state.jobs = payload.jobs || []; renderJobs(); }
    return;
  }
  if (method === "approval/requested") {
    if (payload.sessionId === state.current) addApproval(payload);
    return;
  }
  if (method === "approval/resolved") {
    if (payload.sessionId === state.current) resolveApproval(payload);
  }
}

// ---------- 会话选择与基线 ----------

async function selectSession(sessionId) {
  state.current = sessionId;
  state.events = [];
  state.seenSeqs = new Set();
  state.approvals = new Map();
  state.queue = [];
  state.jobs = [];
  renderEmpty();
  refreshSessions();
  const value = await rpc("session.history", { sessionId: sessionId, maxMessages: 200 });
  for (const entry of value.events || []) {
    const event = entry.event;
    state.seenSeqs.add(event.seq);
    state.events.push(event);
  }
  state.events.sort((a, b) => a.seq - b.seq);
  renderTrajectory();
  openMux();
}

function renderEmpty() {
  $("trajectory").textContent = "";
  $("queue-panel").textContent = "";
  $("jobs-panel").textContent = "";
  $("approval-panel").textContent = "";
}

// ---------- 轨迹渲染（教学简化：线性折叠，无 Overview/虚拟化） ----------

function renderTrajectory() {
  const tray = $("trajectory");
  tray.textContent = "";
  for (const event of state.events) renderEvent(tray, event);
  tray.scrollTop = tray.scrollHeight;
}

function renderEvent(tray, event) {
  const type = event.type;
  const data = event.data || {};
  if (type === "turn/start") {
    const divider = el("div", "turn-divider", "Turn " + data.turn);
    tray.append(divider);
    return;
  }
  if (type === "turn/end") {
    const reason = data.reason;
    const label = reason && reason.kind ? reason.kind : "end";
    tray.append(el("div", "note", "↳ turn end · " + label));
    return;
  }
  if (type === "user/message") {
    tray.append(messageBubble("user", data.content || []));
    return;
  }
  if (type === "assistant/message") {
    tray.append(messageBubble("assistant", data.content || []));
    return;
  }
  if (type === "tool/call") {
    const card = el("div", "tool-card",
      "tool/call " + data.name + "  " + JSON.stringify(parseArgs(data.arguments)));
    tray.append(card);
    return;
  }
  if (type === "tool/result") {
    const card = el("div", "tool-card" + (data.isError ? " error" : ""),
      (data.isError ? "✕ tool/result " : "✓ tool/result ") + contentText(data.content));
    tray.append(card);
    return;
  }
  if (type === "compaction/start" || type === "compaction/end") {
    tray.append(el("div", "note", type));
    return;
  }
  if (type === "compaction/summary") {
    tray.append(el("div", "note", "compaction/summary · " + contentText(data.content)));
    return;
  }
  if (type === "approval/asked" || type === "approval/decided") {
    tray.append(el("div", "note", type + (data.outcome ? " · " + data.outcome : "")));
  }
}

function messageBubble(role, blocks) {
  const text = blocks
    .map((block) => {
      if (block.type === "text") return block.text;
      if (block.type === "reasoning") return "";
      if (block.type === "image") return "[image]";
      if (block.type === "tool-call") return "→ tool " + block.name;
      if (block.type === "tool-result") return "← tool result";
      return "";
    })
    .filter((part) => part !== "")
    .join("\n");
  return el("div", "msg " + role, el("div", "bubble", text || "(空消息)"));
}

function parseArgs(argumentsText) {
  try { return JSON.parse(argumentsText || "{}"); } catch { return argumentsText || {}; }
}

function contentText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map(contentText).join(" ");
  return JSON.stringify(content);
}

// ---------- 审批面板 ----------

function addApproval(payload) {
  const card = el("div", "approval-card");
  card.append(
    el("div", "tool", "等待审批 · " + payload.toolName),
    el("div", "approval-id", "approval " + payload.approvalId.slice(0, 8) + " · " + payload.sessionId.slice(0, 12))
  );
  const actions = el("div", "actions");
  const allow = el("button", "allow", "Allow once");
  const reject = el("button", "reject", "Reject");
  allow.addEventListener("click", () => answer(payload, "allowed-once"));
  reject.addEventListener("click", () => answer(payload, "rejected"));
  actions.append(allow, reject);
  card.append(actions);
  $("approval-panel").append(card);
  state.approvals.set(payload.rpcId, card);
}

async function answer(payload, outcome) {
  await respond(payload.rpcId, {
    sessionId: payload.sessionId,
    approvalId: payload.approvalId,
    outcome: outcome,
  });
}

function resolveApproval(payload) {
  const card = state.approvals.get(payload.rpcId);
  if (!card) return;
  card.classList.add("resolved");
  card.querySelector(".tool").textContent = "已" + (payload.outcome === "allowed-once" ? "允许" : "拒绝") + " · " + payload.toolName;
}

// ---------- 队列 / 作业 ----------

function renderQueue() {
  const panel = $("queue-panel");
  panel.textContent = "";
  if (!state.queue.length) { panel.append(el("p", "muted", "队列空")); return; }
  for (const item of state.queue) {
    const node = el("div", "queue-item",
      "[" + item.placement + "] " + (item.message.content || []).map((b) => b.text || "").join(" ").slice(0, 60));
    panel.append(node);
  }
}

function renderJobs() {
  const panel = $("jobs-panel");
  panel.textContent = "";
  if (!state.jobs.length) { panel.append(el("p", "muted", "无作业")); return; }
  for (const job of state.jobs) {
    panel.append(el("div", "job-item", "[" + job.status + "] " + job.kind + " · " + job.label));
  }
}

// ---------- 交互 ----------

async function refreshDescribe() {
  state.describe = await rpc("host.describe", {});
  $("describe").textContent =
    state.describe.version + " · " + state.describe.provider + "/" + state.describe.model +
    " · cwd " + state.describe.cwd;
  $("create-cwd").value = state.describe.cwd;
}

function setupPrompt() {
  $("prompt-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!state.current) return;
    const input = $("prompt-input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    try {
      await rpc("session.prompt", {
        sessionId: state.current,
        mode: "queue",
        content: [{ type: "text", text: text }],
      });
    } catch (error) {
      alert(error.message);
    }
  });
}

function setupCreateDialog() {
  $("btn-create").addEventListener("click", () => $("create-dialog").classList.remove("hidden"));
  $("create-cancel").addEventListener("click", () => $("create-dialog").classList.add("hidden"));
  $("create-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const payload = { cwd: $("create-cwd").value.trim() };
    const id = $("create-id").value.trim();
    if (id) payload.sessionId = id;
    $("create-dialog").classList.add("hidden");
    try {
      const value = await rpc("session.create", payload);
      await refreshSessions(value.sessionId);
    } catch (error) {
      alert(error.message);
    }
  });
}

// ---------- 启动 ----------

async function init() {
  setupPrompt();
  setupCreateDialog();
  try {
    await refreshDescribe();
  } catch (error) {
    $("describe").textContent = error.message;
    return;
  }
  try {
    await refreshSessions();
  } catch (error) {
    $("session-list").append(muted(error.message));
  }
}

init();