// Terminal turn feedback (upstream ui-chat MessageItem `TurnErrorItem` and
// `TurnMaxTokensItem`): a turn that ended at the output-token cap, failed with
// an error reason, or was closed by restart recovery shows one persistent row
// after its messages. Provider codes with product copy are translated; any
// other message is shown verbatim from the session log.

import type { TurnFailure } from "../trajectory/model";

/** Substitute the localized copy for codes the product names itself. */
function failureMessage(failure: TurnFailure): string {
  switch (failure.code) {
    case "ACCOUNT_SIGNED_OUT":
      return "任务已因退出 DeepSeek 登录而停止。";
    case "INTERRUPTED":
      return "DSH 进程在本轮完成前退出（例如被强制结束、崩溃或关闭超时），重新启动后本轮已中止。未完成的工具调用结果未知。";
    case "ACCOUNT_SIGN_IN_REQUIRED":
      return "请先登录 DeepSeek，并确认请求地址支持账号认证。";
    case "QUOTA":
    case "ACCOUNT_QUOTA":
      return "当前请求的额度已用尽";
    case "AUTH":
      return "API 密钥无效";
    default:
      return failure.message;
  }
}

export interface TurnNoticeProps {
  notice: "max-tokens" | "error" | "stopped";
  failure: TurnFailure | null;
}

/** One turn-positioned notice row. */
export function TurnNotice({ notice, failure }: TurnNoticeProps) {
  if (notice === "max-tokens") {
    return (
      <div className="turn-notice" role="status" data-kind="warning">
        <span className="turn-notice-dot" aria-hidden="true" />
        <div className="turn-notice-copy">
          <span className="turn-notice-title">已达到输出 token 上限</span>
          <span className="turn-notice-message">
            回答被截断，已有输出保留在对话中。发送“继续”可让模型接着输出。
          </span>
        </div>
      </div>
    );
  }
  if (failure === null) return null;
  const stopped = notice === "stopped";
  return (
    <div className="turn-notice" role="status" data-kind="error">
      <span className="turn-notice-dot" aria-hidden="true" />
      <div className="turn-notice-copy">
        <span className="turn-notice-title">{stopped ? "任务已停止" : "本轮运行失败"}</span>
        <span className="turn-notice-message">{failureMessage(failure)}</span>
      </div>
      {failure.code !== undefined && failure.code !== "INTERRUPTED" && (
        <code className="turn-notice-code">{failure.code}</code>
      )}
    </div>
  );
}
