import { MarkdownText } from "./MarkdownText";

interface Props {
  text: string;
  matched?: boolean;
  highlightTerms?: readonly string[];
}

export function ReasoningRow({ text, matched = false, highlightTerms }: Props) {
  return (
    <div className={`reasoning-row${matched ? " hit" : ""}`}>
      <MarkdownText text={text} variant="compact" highlightTerms={highlightTerms} />
    </div>
  );
}
