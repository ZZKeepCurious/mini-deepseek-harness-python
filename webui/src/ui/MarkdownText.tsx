import { memo, useMemo } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import type { Components, Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { workspaceFileUrl } from "../wire/workspace";

interface HastNode {
  type: string;
  value?: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

interface HighlightOptions {
  terms: readonly string[];
}

function escapeRe(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function splitText(value: string, pattern: RegExp): HastNode[] {
  const out: HastNode[] = [];
  let last = 0;
  for (const match of value.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > last) out.push({ type: "text", value: value.slice(last, index) });
    out.push({
      type: "element",
      tagName: "mark",
      properties: {},
      children: [{ type: "text", value: match[0] }],
    });
    last = index + match[0].length;
  }
  if (last < value.length) out.push({ type: "text", value: value.slice(last) });
  return out;
}

function rehypeHighlightTerms(options?: HighlightOptions) {
  const terms = (options?.terms ?? []).filter(Boolean);
  const pattern = terms.length > 0
    ? new RegExp(`(${terms.map(escapeRe).join("|")})`, "gi")
    : null;
  return (tree: HastNode) => {
    if (!pattern) return;
    const walk = (node: HastNode) => {
      if (!node.children) return;
      const children: HastNode[] = [];
      for (const child of node.children) {
        if (child.type === "text" && child.value) {
          children.push(...splitText(child.value, new RegExp(pattern.source, pattern.flags)));
        } else {
          walk(child);
          children.push(child);
        }
      }
      node.children = children;
    };
    walk(tree);
  };
}

function imageSource(source: string | undefined): string | undefined {
  if (!source) return undefined;
  if (/^(?:https?:|data:image\/|blob:)/i.test(source)) return source;
  if (/^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(source)) return workspaceFileUrl(source);
  return source;
}

function urlTransform(url: string, key: string): string {
  if ((key === "src" || key === "href") && /^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(url)) {
    return url;
  }
  return defaultUrlTransform(url);
}

const components: Components = {
  a({ node: _node, ...props }) {
    return <a {...props} target="_blank" rel="noreferrer noopener" />;
  },
  img({ node: _node, src, alt, ...props }) {
    return <img {...props} src={imageSource(src)} alt={alt ?? ""} loading="lazy" decoding="async" />;
  },
};

export const MarkdownText = memo(function MarkdownText({
  text,
  variant = "body",
  highlightTerms = [],
}: {
  text: string;
  variant?: "body" | "compact";
  highlightTerms?: readonly string[];
}) {
  const rehypePlugins = useMemo<NonNullable<Options["rehypePlugins"]>>(() => {
    const plugins: NonNullable<Options["rehypePlugins"]> = [[rehypeKatex, { throwOnError: false }]];
    if (highlightTerms.length > 0) {
      plugins.push([rehypeHighlightTerms, { terms: highlightTerms }]);
    }
    return plugins;
  }, [highlightTerms]);
  return (
    <div
      className={`markdown-body${variant === "compact" ? " compact" : ""}`}
      data-markdown-variant={variant === "compact" ? "compact" : undefined}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={rehypePlugins}
        components={components}
        urlTransform={urlTransform}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
