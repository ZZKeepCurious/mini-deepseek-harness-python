import { useMemo, useState } from "react";
import type { ModelCatalog, ModelSelection } from "../wire/session";

interface Props {
  catalog: ModelCatalog | null;
  selection: ModelSelection | null;
  locked: boolean;
  onSelect: (provider: string, model: string) => Promise<string | null>;
}

function labelOf(selection: ModelSelection | null): string {
  if (selection === null) return "选择模型";
  return `${selection.provider}/${selection.model}`;
}

export function ModelSelect({ catalog, selection, locked, onSelect }: Props) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const groups = catalog?.groups ?? [];

  const flat = useMemo(
    () =>
      groups.flatMap((group) =>
        group.models.map((model) => ({
          provider: group.id,
          model: model.id,
          label: model.name,
        })),
      ),
    [groups],
  );

  const current = labelOf(selection);

  return (
    <div className="composer-model-select">
      <button
        type="button"
        className="btn small ghost"
        disabled={locked || flat.length === 0}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Select model, current ${current}`}
        onClick={() => {
          setError(null);
          setOpen((value) => !value);
        }}
      >
        {current}
      </button>
      {error !== null && (
        <span className="composer-model-error" role="alert">
          {error}
        </span>
      )}
      {open && (
        <div className="composer-model-menu" role="listbox">
          {flat.map((entry) => (
            <button
              key={`${entry.provider}:${entry.model}`}
              type="button"
              role="option"
              className="composer-model-option"
              aria-selected={
                selection?.provider === entry.provider && selection?.model === entry.model
              }
              onClick={() => {
                void onSelect(entry.provider, entry.model).then((message) => {
                  if (message !== null) setError(message);
                  else setOpen(false);
                });
              }}
            >
              <span>{entry.label}</span>
              <span className="dim">{entry.provider}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
