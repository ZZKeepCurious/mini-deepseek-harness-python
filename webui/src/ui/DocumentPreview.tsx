import { useEffect, useState } from "react";
import type { WorkspaceReadResult, WorkspaceServices, WorkspaceStatResult } from "../wire/workspace";
import { workspaceFileUrl } from "../wire/workspace";
import { renderOfficeToPdf } from "../wire/officeToPdf";
import { RpcFailure } from "../wire/rpc";
import { MarkdownText } from "./MarkdownText";

interface Props {
  sessionId: string;
  path: string;
  services: WorkspaceServices;
  onClose: () => void;
}

const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "gif", "webp"]);
const MARKDOWN_EXTENSIONS = new Set(["md", "mdx", "markdown"]);
const OFFICE_EXTENSIONS = new Set(["doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp"]);

function extensionOf(path: string): string {
  const name = path.slice(path.lastIndexOf("/") + 1);
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot + 1).toLowerCase();
}

function previewKind(path: string): "image" | "pdf" | "office" | "markdown" | "text" {
  const extension = extensionOf(path);
  if (IMAGE_EXTENSIONS.has(extension)) return "image";
  if (extension === "pdf") return "pdf";
  if (OFFICE_EXTENSIONS.has(extension)) return "office";
  if (MARKDOWN_EXTENSIONS.has(extension)) return "markdown";
  return "text";
}

export function DocumentPreview({ sessionId, path, services, onClose }: Props) {
  const [text, setText] = useState<WorkspaceReadResult | null>(null);
  const [stat, setStat] = useState<WorkspaceStatResult | null>(null);
  const [officePdfUrl, setOfficePdfUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const kind = previewKind(path);

  useEffect(() => {
    let cancelled = false;
    setText(null);
    setStat(null);
    setOfficePdfUrl(null);
    setError(null);
    if (kind === "office") {
      void renderOfficeToPdf(sessionId, path, "foreground")
        .then((result) => {
          if (cancelled) return;
          const blob = new Blob([new Uint8Array(result.data)], { type: "application/pdf" });
          setOfficePdfUrl(URL.createObjectURL(blob));
        })
        .catch((cause: unknown) => {
          if (cancelled) return;
          if (cause instanceof RpcFailure) {
            setError(cause.message);
            return;
          }
          setError(cause instanceof Error ? cause.message : String(cause));
        });
      return () => {
        cancelled = true;
      };
    }
    if (kind === "image" || kind === "pdf") {
      void services.stat(sessionId, path)
        .then((result) => {
          if (!cancelled) setStat(result);
        })
        .catch((cause: unknown) => {
          if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
        });
      return () => {
        cancelled = true;
      };
    }
    void services.read(sessionId, path)
      .then((result) => {
        if (!cancelled) setText(result);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [kind, path, services, sessionId]);

  useEffect(() => {
    return () => {
      if (officePdfUrl !== null) URL.revokeObjectURL(officePdfUrl);
    };
  }, [officePdfUrl]);

  return (
    <div className="document-preview">
      <div className="preview-head">
        <span className="preview-path" title={path}>{path}</span>
        <span className="spacer" />
        <button type="button" className="btn small ghost" onClick={onClose}>关闭</button>
      </div>
      <div className="preview-body">
        {error !== null && <div className="file-error" role="alert">{error}</div>}
        {kind === "office" && officePdfUrl === null && error === null && (
          <div className="empty dim">正在转换为 PDF…</div>
        )}
        {kind !== "office" && text === null && stat === null && error === null && (
          <div className="empty dim">加载中…</div>
        )}
        {kind === "markdown" && text !== null && (
          <div className="preview-markdown">
            <MarkdownText text={text.text} variant="body" />
          </div>
        )}
        {kind === "text" && text !== null && (
          <pre className="preview-text">{text.text}</pre>
        )}
        {kind === "image" && stat !== null && (
          <div className="preview-image">
            <img src={workspaceFileUrl(stat.absolutePath)} alt={path} />
          </div>
        )}
        {kind === "pdf" && stat !== null && (
          <iframe className="preview-frame" title={path} src={workspaceFileUrl(stat.absolutePath)} />
        )}
        {kind === "office" && officePdfUrl !== null && (
          <iframe className="preview-frame" title={path} src={officePdfUrl} />
        )}
        {text !== null && !text.eof && <div className="preview-note">仅显示前 {text.lines} 行</div>}
        {stat !== null && stat.bytes !== undefined && kind !== "office" && (
          <div className="preview-note">{stat.bytes} bytes</div>
        )}
      </div>
    </div>
  );
}
