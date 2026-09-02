// Shared fixture builders for trajectory model/search/component tests.

import type { EventEnvelope } from "../src/wire";

function ev(seq: number, type: string, data: Record<string, unknown>): EventEnvelope {
  return { type, seq, data };
}

export function turnStart(seq: number, turn: number): EventEnvelope {
  return ev(seq, "turn/start", { turn });
}

export function turnEnd(seq: number): EventEnvelope {
  return ev(seq, "turn/end", {});
}

export function userMsg(seq: number, text: string): EventEnvelope {
  return ev(seq, "user/message", {
    message: { role: "user", content: [{ type: "text", text }] },
  });
}

export function assistantMsg(seq: number, text: string, reasoning?: string): EventEnvelope {
  const content = [
    ...(reasoning ? [{ type: "reasoning", text: reasoning }] : []),
    { type: "text", text },
  ];
  return ev(seq, "assistant/message", { message: { role: "assistant", content } });
}

export function toolCall(seq: number, name: string, args: string): EventEnvelope {
  return ev(seq, "tool/call", { name, arguments: args });
}

export function toolResult(seq: number, content: string, isError = false): EventEnvelope {
  return ev(seq, "tool/result", { content, isError });
}

// A two-turn session with a merged tool call, a reasoning block and a couple of
// control-plane events that must be ignored by the display model.
export function standardSession(): EventEnvelope[] {
  const events: EventEnvelope[] = [];
  let seq = 0;
  const push = (e: EventEnvelope) => events.push(e);
  push(turnStart(++seq, 1));
  push(userMsg(++seq, "你好世界"));
  push(assistantMsg(++seq, "回答一下问题", "思考一下"));
  push(toolCall(++seq, "git", '{"cmd":"status"}'));
  push(toolResult(++seq, "clean", false));
  push(ev(++seq, "agent/created", { agent: { id: "a1" } }));
  push(turnEnd(++seq));
  push(turnStart(++seq, 2));
  push(userMsg(++seq, "再查下日志"));
  push(assistantMsg(++seq, "好的"));
  push(turnEnd(++seq));
  return events;
}

export function bigTurn(messages: number): EventEnvelope[] {
  const events: EventEnvelope[] = [turnStart(1, 1)];
  for (let i = 0; i < messages; i++) {
    events.push(userMsg(2 + i, `消息 ${i} 的内容请带上一些足够长的文字以便估算高度`));
  }
  events.push(turnEnd(2 + messages));
  return events;
}