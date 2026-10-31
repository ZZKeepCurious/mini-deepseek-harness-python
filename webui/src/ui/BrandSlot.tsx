interface Props {
  productName?: string;
}

export function BrandSlot({ productName = "DeepSeek Harness" }: Props) {
  return (
    <div className="brand-slot" aria-label="产品标识">
      <span className="brand-mark" aria-hidden="true">
        DS
      </span>
      <span className="brand-name">{productName}</span>
    </div>
  );
}
