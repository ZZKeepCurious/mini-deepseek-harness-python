interface Props {
  hasMore: boolean;
  onLoad: () => void;
}

export function HistoryPager({ hasMore, onLoad }: Props) {
  if (!hasMore) return null;
  return (
    <div className="history-pager">
      <button type="button" className="btn small ghost" onClick={onLoad}>
        加载更早消息
      </button>
    </div>
  );
}
