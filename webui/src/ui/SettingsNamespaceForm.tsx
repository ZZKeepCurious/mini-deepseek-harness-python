// Generic namespace form over the settings wire.
//
// One namespace's config is rendered field by field from its resolved value.
// Writes are immediate path-addressed mutations fenced by the namespace
// revision the host answered (upstream config-form `set`/`unset`); a field the
// user layer overrides offers "恢复默认" (unset). Secret paths arrive redacted,
// so they render as presence-only masked inputs.

import { useEffect, useState } from "react";
import type {
  SettingsNamespaceView,
  SettingsPath,
  SettingsPathOp,
} from "../wire";

export interface SettingsNamespaceFormProps {
  view: SettingsNamespaceView;
  writable: boolean;
  busy: boolean;
  onMutate: (
    ns: string,
    ops: SettingsPathOp[],
    revision?: number,
  ) => Promise<SettingsNamespaceView | null>;
}

type FieldKind = "boolean" | "number" | "string" | "json";

interface Field {
  path: SettingsPath;
  label: string;
  kind: FieldKind;
  value: unknown;
  overridden: boolean;
  secret: boolean;
  secretSet: boolean;
  title?: string;
  description?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function pathKey(path: SettingsPath): string {
  return JSON.stringify(path);
}

function pathLabel(path: SettingsPath): string {
  return path.length === 0 ? "(根)" : path.join(".");
}

function hasPath(root: unknown, path: SettingsPath): boolean {
  let node: unknown = root;
  for (const part of path) {
    if (Array.isArray(node)) {
      if (typeof part !== "number" || part < 0 || part >= node.length) return false;
      node = node[part];
      continue;
    }
    if (!isRecord(node) || typeof part !== "string" || !(part in node)) return false;
    node = node[part];
  }
  return true;
}

function kindOf(value: unknown): FieldKind {
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "number") return "number";
  if (typeof value === "string") return "string";
  return "json";
}

function schemaNode(schema: unknown, key: string): Record<string, unknown> | null {
  if (!isRecord(schema)) return null;
  const properties = schema.properties;
  if (!isRecord(properties)) return null;
  const node = properties[key];
  return isRecord(node) ? node : null;
}

function stringField(node: Record<string, unknown> | null, name: string): string | undefined {
  const value = node?.[name];
  return typeof value === "string" ? value : undefined;
}

/** Project one namespace value plus its secret paths onto renderable fields. */
export function settingsFields(view: SettingsNamespaceView): Field[] {
  const secrets = new Map(view.secrets.map((secret) => [pathKey(secret.path), secret.set]));
  const fields: Field[] = [];

  const push = (path: SettingsPath, value: unknown): void => {
    const node = path.length === 1 ? schemaNode(view.schema, String(path[0])) : null;
    fields.push({
      path,
      label: pathLabel(path),
      kind: kindOf(value),
      value,
      overridden: hasPath(view.user, path),
      secret: secrets.has(pathKey(path)),
      secretSet: secrets.get(pathKey(path)) ?? false,
      title: stringField(node, "title"),
      description: stringField(node, "description"),
    });
  };

  if (isRecord(view.value)) {
    for (const [key, value] of Object.entries(view.value)) push([key], value);
    const properties = isRecord(view.schema.properties) ? view.schema.properties : {};
    for (const key of Object.keys(properties)) {
      if (!(key in view.value)) push([key], undefined);
    }
  } else if (view.value !== undefined && view.value !== null) {
    push([], view.value);
  }

  for (const secret of view.secrets) {
    if (fields.some((field) => pathKey(field.path) === pathKey(secret.path))) continue;
    push(secret.path, undefined);
    const added = fields[fields.length - 1];
    added.kind = "string";
  }

  return fields;
}

function formatText(value: unknown): string {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : String(value);
}

function formatJson(value: unknown): string {
  if (value === undefined) return "";
  return JSON.stringify(value, null, 2);
}

export function SettingsNamespaceForm({
  view,
  writable,
  busy,
  onMutate,
}: SettingsNamespaceFormProps) {
  const fields = settingsFields(view);
  const editable = writable && !busy;

  const write = (ops: SettingsPathOp[]): void => {
    void onMutate(view.ns, ops, view.revision);
  };

  if (fields.length === 0) {
    return <div className="empty settings-form-empty">该插件没有可配置项。</div>;
  }

  return (
    <div className="settings-form">
      {fields.map((field) => (
        <SettingsField
          key={`${pathKey(field.path)}@${view.revision}`}
          field={field}
          editable={editable}
          onWrite={write}
        />
      ))}
      <div className="settings-form-meta">
        <span>revision {view.revision}</span>
        <span>{view.applies === "live" ? "即时生效" : view.applies}</span>
      </div>
    </div>
  );
}

interface FieldProps {
  field: Field;
  editable: boolean;
  onWrite: (ops: SettingsPathOp[]) => void;
}

function SettingsField({ field, editable, onWrite }: FieldProps) {
  const text = field.title ?? field.label;
  return (
    <div className="settings-field">
      <div className="settings-field-text">
        <div className="settings-field-title">{text}</div>
        {field.description !== undefined && (
          <div className="settings-field-desc">{field.description}</div>
        )}
      </div>
      <div className="settings-field-control">
        {field.kind === "boolean" && (
          <SwitchControl field={field} editable={editable} onWrite={onWrite} />
        )}
        {field.kind === "number" && (
          <NumberControl field={field} editable={editable} onWrite={onWrite} />
        )}
        {field.kind === "string" && (
          <TextControl field={field} editable={editable} onWrite={onWrite} />
        )}
        {field.kind === "json" && (
          <JsonControl field={field} editable={editable} onWrite={onWrite} />
        )}
        <div className="settings-field-state">
          {field.overridden && <span className="settings-badge">已覆盖</span>}
          {field.overridden && (
            <button
              type="button"
              className="btn small ghost"
              disabled={!editable}
              onClick={() => onWrite([{ op: "unset", path: field.path }])}
            >
              恢复默认
            </button>
          )}
          {field.secret && (
            <button
              type="button"
              className="btn small ghost"
              disabled={!editable || !field.secretSet}
              onClick={() => onWrite([{ op: "unset", path: field.path }])}
            >
              清除密钥
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function SwitchControl({ field, editable, onWrite }: FieldProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={field.value === true}
      aria-label={field.title ?? field.label}
      className={`switch${field.value === true ? " on" : ""}`}
      disabled={!editable}
      onClick={() => onWrite([{ op: "set", path: field.path, value: field.value !== true }])}
    >
      <span />
    </button>
  );
}

function NumberControl({ field, editable, onWrite }: FieldProps) {
  const [draft, setDraft] = useState(() => formatText(field.value));
  const [invalid, setInvalid] = useState(false);

  const commit = (): void => {
    const parsed = Number(draft);
    if (draft.trim() === "" || !Number.isFinite(parsed)) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    if (parsed === field.value) return;
    onWrite([{ op: "set", path: field.path, value: parsed }]);
  };

  return (
    <div className="settings-number">
      <input
        className="input"
        type="number"
        aria-label={field.title ?? field.label}
        value={draft}
        disabled={!editable}
        onChange={(event) => setDraft(event.currentTarget.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") commit();
        }}
      />
      {invalid && <span className="settings-field-error">请输入数字</span>}
    </div>
  );
}

function TextControl({ field, editable, onWrite }: FieldProps) {
  const [draft, setDraft] = useState(() => formatText(field.value));
  const placeholder = field.secret
    ? field.secretSet
      ? "已设置（输入新值替换）"
      : "未设置"
    : "";

  const commit = (): void => {
    if (field.secret && draft === "") return;
    if (draft === formatText(field.value)) return;
    onWrite([{ op: "set", path: field.path, value: draft }]);
  };

  return (
    <input
      className="input"
      type={field.secret ? "password" : "text"}
      aria-label={field.title ?? field.label}
      placeholder={placeholder}
      value={draft}
      disabled={!editable}
      onChange={(event) => setDraft(event.currentTarget.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === "Enter") commit();
      }}
    />
  );
}

function JsonControl({ field, editable, onWrite }: FieldProps) {
  const original = formatJson(field.value);
  const [draft, setDraft] = useState(original);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    setDraft(original);
    setProblem(null);
  }, [original]);

  const commit = (): void => {
    if (draft === original) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(draft);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : "JSON 解析失败");
      return;
    }
    setProblem(null);
    onWrite([{ op: "set", path: field.path, value: parsed }]);
  };

  return (
    <div className="settings-json">
      <textarea
        className="input settings-json-input"
        aria-label={field.title ?? field.label}
        rows={Math.min(12, Math.max(3, draft.split("\n").length))}
        value={draft}
        disabled={!editable}
        onChange={(event) => setDraft(event.currentTarget.value)}
      />
      <div className="settings-json-actions">
        <button
          type="button"
          className="btn small"
          disabled={!editable || draft === original}
          onClick={commit}
        >
          应用
        </button>
        {problem !== null && <span className="settings-field-error">{problem}</span>}
      </div>
    </div>
  );
}
