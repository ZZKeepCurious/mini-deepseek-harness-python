import { useState } from "react";
import type { ToolRow } from "../../trajectory/model";
import {
  diffModel,
  imageModel,
  readModel,
  searchModel,
  shellModel,
  todoModel,
  webFetchModel,
  webSearchModel,
} from "../../trajectory/tool-views";
import type { DiffModel, ReadModel, SearchModel, TodoModel, WebModel } from "../../trajectory/tool-views";
import { Highlight } from "../Highlight";
import { MarkdownText } from "../MarkdownText";

interface Props {
  row: ToolRow;
  terms: string[];
}

function firstLine(value: string): string {
  const line = value.split(/\r?\n/u).find((part) => part.trim() !== "");
  return line?.trim() ?? "";
}

function CopyButton({ value }: { value: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async () => {
    if (navigator.clipboard === undefined) {
      setState("failed");
      return;
    }
    try {
      await navigator.clipboard.writeText(value);
      setState("copied");
    } catch {
      setState("failed");
    }
  };
  return (
    <button type="button" className="btn small ghost" onClick={() => void copy()}>
      {state === "copied" ? "已复制" : state === "failed" ? "复制失败" : "复制"}
    </button>
  );
}

function RawDetails({ row }: { row: ToolRow }) {
  return (
    <pre className="tool-raw">
      {JSON.stringify(
        { arguments: row.arguments, result: row.result, meta: row.meta },
        null,
        2,
      )}
    </pre>
  );
}

function ShellCard({ row, terms }: Props) {
  const model = shellModel(row);
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  if (model === null) return <GenericCard row={row} terms={terms} />;
  const title = row.name === "pwsh" ? "PowerShell" : "Bash";
  const summary = model.description || firstLine(model.command);
  return (
    <div className={`tool-card shell${row.isError ? " error" : ""}`}>
      <button type="button" className="tool-card-head" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <span className="tool-card-title">{title}</span>
        <span className="tool-card-summary">
          <Highlight text={summary} terms={terms} />
        </span>
        <span className="tool-card-state">{model.summary}</span>
      </button>
      {open && (
        <div className="tool-card-body">
          <div className="tool-command">
            {model.cwd !== "" && <span className="tool-cwd">{model.cwd}</span>}
            <span className="tool-command-text">{model.command}</span>
          </div>
          {model.output !== "" && (
            <pre className="tool-output">
              <Highlight text={model.output} terms={terms} />
            </pre>
          )}
          {model.stderr !== "" && (
            <pre className="tool-stderr">
              <Highlight text={model.stderr} terms={terms} />
            </pre>
          )}
          <div className="tool-card-actions">
            <CopyButton value={model.body || model.command} />
            <button type="button" className="btn small ghost" onClick={() => setRaw((current) => !current)}>
              {raw ? "收起原始" : "查看原始"}
            </button>
          </div>
          {raw && <RawDetails row={row} />}
        </div>
      )}
    </div>
  );
}

function SearchGroups({ model, terms }: { model: SearchModel; terms: string[] }) {
  if (model.kind === "paths") {
    return (
      <ul className="tool-path-list">
        {model.paths.map((path) => (
          <li key={path}>
            <Highlight text={path} terms={terms} />
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div className="tool-search-groups">
      {model.groups.map((group) => (
        <div className="tool-search-group" key={group.path}>
          <div className="tool-search-path">
            <Highlight text={group.path} terms={terms} />
          </div>
          {group.matches.map((match) => (
            <div className="tool-search-line" key={`${group.path}:${match.lineNumber}`}>
              <span className="tool-search-number">{match.lineNumber}</span>
              <span>
                <Highlight text={match.line} terms={terms} />
              </span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function SearchCard({ row, terms }: Props) {
  const model = searchModel(row);
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  if (model === null) return <GenericCard row={row} terms={terms} />;
  const title = row.name === "glob" ? "Glob" : "Grep";
  return (
    <div className={`tool-card search${row.isError ? " error" : ""}`}>
      <button type="button" className="tool-card-head" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <span className="tool-card-title">{title}</span>
        <span className="tool-card-summary">{model.summary}</span>
        <span className="tool-card-state">{row.isError ? "失败" : "完成"}</span>
      </button>
      {open && (
        <div className="tool-card-body">
          <SearchGroups model={model} terms={terms} />
          {model.recovery !== null && <div className="tool-search-recovery">{model.recovery}</div>}
          <div className="tool-card-actions">
            <CopyButton value={row.result} />
            <button type="button" className="btn small ghost" onClick={() => setRaw((current) => !current)}>
              {raw ? "收起原始" : "查看原始"}
            </button>
          </div>
          {raw && <RawDetails row={row} />}
        </div>
      )}
    </div>
  );
}

function TodoItems({ model }: { model: TodoModel }) {
  const labels = {
    completed: "已完成",
    in_progress: "进行中",
    pending: "待办",
  } as const;
  return (
    <div className="tool-todo">
      <div className="tool-todo-counts">
        {model.completed} 已完成 · {model.inProgress} 进行中 · {model.pending} 待办
      </div>
      <ul className="tool-todo-list">
        {model.items.map((item) => (
          <li className={`tool-todo-item ${item.status}`} key={item.content}>
            <span className="tool-todo-status">{labels[item.status]}</span>
            <span>{item.content}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function TodoCard({ row }: { row: ToolRow }) {
  const model = todoModel(row);
  const [open, setOpen] = useState(false);
  if (model === null) return <GenericCard row={row} terms={[]} />;
  const summary = `${model.done}/${model.total} 已完成${model.activeContent === null ? "" : ` · ${model.activeContent}`}`;
  return (
    <div className={`tool-card todo${row.isError ? " error" : ""}`}>
      <button type="button" className="tool-card-head" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <span className="tool-card-title">更新待办清单</span>
        <span className="tool-card-summary">{summary}</span>
        {model.activeExtra > 0 && <span className="tool-card-suffix">+{model.activeExtra}</span>}
        <span className="tool-card-state">{row.isError ? "失败" : "完成"}</span>
      </button>
      {open && (
        <div className="tool-card-body">
          <TodoItems model={model} />
        </div>
      )}
    </div>
  );
}

function ReadLines({ model }: { model: ReadModel }) {
  return (
    <div className="tool-read-lines">
      {model.lines.map((line) => (
        <div className="tool-read-line" key={line.number}>
          <span className="tool-read-number">{line.number}</span>
          <span>{line.text}</span>
        </div>
      ))}
    </div>
  );
}

function ReadCard({ row, terms }: Props) {
  const model = readModel(row);
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  if (model === null) return <GenericCard row={row} terms={terms} />;
  const end = model.lines[model.lines.length - 1]?.number ?? Math.max(0, model.offset - 1);
  const text = model.lines.map((line) => `${line.number}: ${line.text}`).join("\n");
  return (
    <div className={`tool-card read${row.isError ? " error" : ""}`}>
      <button type="button" className="tool-card-head" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <span className="tool-card-title">读取文件</span>
        <span className="tool-card-summary">{model.path}</span>
        <span className="tool-card-state">{row.isError ? "失败" : `行 ${model.offset}-${end} / ${model.totalLines}`}</span>
      </button>
      {open && (
        <div className="tool-card-body">
          <div className="tool-read-meta">
            {model.offset}-{end} / {model.totalLines}
            {model.lang === null ? "" : ` · ${model.lang}`}
          </div>
          <ReadLines model={model} />
          <div className="tool-card-actions">
            <CopyButton value={text} />
            <button type="button" className="btn small ghost" onClick={() => setRaw((current) => !current)}>
              {raw ? "收起原始" : "查看原始"}
            </button>
          </div>
          {raw && <RawDetails row={row} />}
        </div>
      )}
    </div>
  );
}

function DiffHunks({ model }: { model: DiffModel }) {
  if (model.hunks.length === 0) {
    return <div className="tool-diff-empty">{model.operation === "create" ? "已创建文件" : "内容未变化"}</div>;
  }
  return (
    <div className="tool-diff-hunks">
      {model.hunks.map((hunk, index) => (
        <div className="tool-diff-hunk" key={`${hunk.path}:${index}`}>
          <div className="tool-diff-path">{hunk.path}</div>
          {hunk.oldText !== null && (
            <pre className="tool-diff-old">{hunk.oldText.split("\n").map((line) => `- ${line}`).join("\n")}</pre>
          )}
          <pre className="tool-diff-new">{hunk.newText.split("\n").map((line) => `+ ${line}`).join("\n")}</pre>
        </div>
      ))}
    </div>
  );
}

function DiffCard({ row }: { row: ToolRow }) {
  const model = diffModel(row);
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  if (model === null) return <GenericCard row={row} terms={[]} />;
  const title = row.name === "edit" ? "编辑文件" : "写入文件";
  const operation = model.operation === "create" ? "创建" : "更新";
  return (
    <div className={`tool-card diff${row.isError ? " error" : ""}`}>
      <button type="button" className="tool-card-head" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <span className="tool-card-title">{title}</span>
        <span className="tool-card-summary">{model.path}</span>
        <span className="tool-card-state">{row.isError ? "失败" : operation}</span>
      </button>
      {open && (
        <div className="tool-card-body">
          <DiffHunks model={model} />
          <div className="tool-card-actions">
            <CopyButton value={row.result} />
            <button type="button" className="btn small ghost" onClick={() => setRaw((current) => !current)}>
              {raw ? "收起原始" : "查看原始"}
            </button>
          </div>
          {raw && <RawDetails row={row} />}
        </div>
      )}
    </div>
  );
}

function ImageCard({ row }: { row: ToolRow }) {
  const model = imageModel(row);
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  if (model === null) return <GenericCard row={row} terms={[]} />;
  return (
    <div className={`tool-card image${row.isError ? " error" : ""}`}>
      <button type="button" className="tool-card-head" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <span className="tool-card-title">读取图片</span>
        <span className="tool-card-summary">{model.path}</span>
        <span className="tool-card-state">{row.isError ? "失败" : "完成"}</span>
      </button>
      {open && (
        <div className="tool-card-body">
          <pre className="tool-output">{model.detail}</pre>
          <div className="tool-card-actions">
            <CopyButton value={model.detail} />
            <button type="button" className="btn small ghost" onClick={() => setRaw((current) => !current)}>
              {raw ? "收起原始" : "查看原始"}
            </button>
          </div>
          {raw && <RawDetails row={row} />}
        </div>
      )}
    </div>
  );
}

function WebCard({ row, model }: { row: ToolRow; model: WebModel }) {
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  const title = row.name === "web_search" ? "Web 搜索" : "Web 抓取";
  return (
    <div className={`tool-card web${row.isError ? " error" : ""}`}>
      <button type="button" className="tool-card-head" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <span className="tool-card-title">{title}</span>
        <span className="tool-card-summary">{model.summary}</span>
        <span className="tool-card-state">{row.isError ? "失败" : "完成"}</span>
      </button>
      {open && (
        <div className="tool-card-body">
          <div className="tool-web-body">
            <MarkdownText text={model.text} variant="compact" />
          </div>
          <div className="tool-card-actions">
            <CopyButton value={model.text} />
            <button type="button" className="btn small ghost" onClick={() => setRaw((current) => !current)}>
              {raw ? "收起原始" : "查看原始"}
            </button>
          </div>
          {raw && <RawDetails row={row} />}
        </div>
      )}
    </div>
  );
}

function GenericCard({ row, terms }: Props) {
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  const summary = row.isError
    ? firstLine(row.result) || "工具失败"
    : firstLine(row.result) || firstLine(row.arguments) || "已完成";
  return (
    <div className={`tool-card generic${row.isError ? " error" : ""}`}>
      <button type="button" className="tool-card-head" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <span className="tool-card-title">{row.name}</span>
        <span className="tool-card-summary">
          <Highlight text={summary} terms={terms} />
        </span>
        <span className="tool-card-state">{row.isError ? "失败" : row.result === "" ? "运行中" : "完成"}</span>
      </button>
      {open && (
        <div className="tool-card-body">
          <pre className="tool-input">
            <Highlight text={row.arguments} terms={terms} />
          </pre>
          {row.result !== "" && (
            <pre className={`tool-output${row.isError ? " error" : ""}`}>
              <Highlight text={row.result} terms={terms} />
            </pre>
          )}
          <div className="tool-card-actions">
            <CopyButton value={row.result} />
            <button type="button" className="btn small ghost" onClick={() => setRaw((current) => !current)}>
              {raw ? "收起原始" : "查看原始"}
            </button>
          </div>
          {raw && <RawDetails row={row} />}
        </div>
      )}
    </div>
  );
}

export function ToolCallView({ row, terms }: Props) {
  if (row.name === "bash" || row.name === "pwsh") return <ShellCard row={row} terms={terms} />;
  if (row.name === "grep" || row.name === "glob") return <SearchCard row={row} terms={terms} />;
  if (row.name === "todo_write") return <TodoCard row={row} />;
  if (row.name === "read") return <ReadCard row={row} terms={terms} />;
  if (row.name === "write" || row.name === "edit") return <DiffCard row={row} />;
  if (row.name === "read_image") return <ImageCard row={row} />;
  if (row.name === "web_search") {
    const model = webSearchModel(row);
    if (model !== null) return <WebCard row={row} model={model} />;
  }
  if (row.name === "web_fetch") {
    const model = webFetchModel(row);
    if (model !== null) return <WebCard row={row} model={model} />;
  }
  return <GenericCard row={row} terms={terms} />;
}