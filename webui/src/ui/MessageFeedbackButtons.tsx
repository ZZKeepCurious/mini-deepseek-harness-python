import type { MessageRating } from "../wire/feedback";

interface Props {
  messageId: string;
  rating?: MessageRating;
  disabled?: boolean;
  onRate: (messageId: string, rating: MessageRating) => void;
  onClear: (messageId: string) => void;
}

export function MessageFeedbackButtons({
  messageId,
  rating,
  disabled,
  onRate,
  onClear,
}: Props) {
  return (
    <span className="message-feedback">
      <button
        type="button"
        className={`btn tiny ghost${rating === "up" ? " active" : ""}`}
        aria-label="有帮助"
        disabled={disabled}
        onClick={() => {
          if (rating === "up") onClear(messageId);
          else onRate(messageId, "up");
        }}
      >
        ↑
      </button>
      <button
        type="button"
        className={`btn tiny ghost${rating === "down" ? " active" : ""}`}
        aria-label="无帮助"
        disabled={disabled}
        onClick={() => {
          if (rating === "down") onClear(messageId);
          else onRate(messageId, "down");
        }}
      >
        ↓
      </button>
    </span>
  );
}
