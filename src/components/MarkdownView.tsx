import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import { useState } from 'react';
import { errorMessage } from '../api';
import remarkGfm from 'remark-gfm';
import type { NoteSummary } from '../types';

type MarkdownNode = { type: string; value?: string; url?: string; children?: MarkdownNode[] };
const WIKI_PREFIX = '#businessmap-note=';
function remarkWikiLinks() {
  return (tree: MarkdownNode) => {
    const visit = (node: MarkdownNode) => {
      if (!node.children || ['code', 'inlineCode', 'link', 'linkReference', 'html'].includes(node.type)) return;
      node.children = node.children.flatMap(child => {
        if (child.type !== 'text' || !child.value) { visit(child); return [child]; }
        const parts: MarkdownNode[] = [];
        const pattern = /\[\[([^\]\n]+)\]\]/g;
        let match: RegExpExecArray | null;
        let cursor = 0;
        while ((match = pattern.exec(child.value)) !== null) {
          if (match.index > cursor) parts.push({ type: 'text', value: child.value.slice(cursor, match.index) });
          const [target, ...aliases] = match[1].split('|');
          parts.push({ type: 'link', url: `${WIKI_PREFIX}${encodeURIComponent(target.trim())}`, children: [{ type: 'text', value: aliases.join('|') || target.trim() }] });
          cursor = match.index + match[0].length;
        }
        if (!cursor) return [child];
        if (cursor < child.value.length) parts.push({ type: 'text', value: child.value.slice(cursor) });
        return parts;
      });
    };
    visit(tree);
  };
}
const plugins = [remarkGfm, remarkWikiLinks];
export function stripFrontmatter(content: string) { return content.replace(/^---\s*\r?\n[\s\S]*?\r?\n---\s*\r?\n/, ''); }
function normalizePath(path: string): string | null {
  const segments: string[] = [];
  for (const segment of path.replace(/\\/g, '/').split('/')) {
    if (!segment || segment === '.') continue;
    if (segment === '..') { if (!segments.length) return null; segments.pop(); }
    else segments.push(segment);
  }
  return segments.join('/');
}
function resolveNote(target: string, notes: NoteSummary[], currentPath?: string): { note?: NoteSummary; heading?: string; ambiguous?: boolean } {
  const hash = target.indexOf('#');
  const name = (hash >= 0 ? target.slice(0, hash) : target).trim();
  const heading = hash >= 0 ? target.slice(hash + 1) : undefined;
  if (!name && currentPath) return { note: notes.find(note => note.path === currentPath), heading };
  const file = name.endsWith('.md') ? name : `${name}.md`;
  const exact = normalizePath(file);
  const relative = currentPath ? normalizePath(`${currentPath.split('/').slice(0, -1).join('/')}/${file}`) : null;
  const paths = new Set([exact, relative].filter(Boolean));
  const matches = notes.filter(note => paths.has(note.path));
  if (matches.length === 1) return { note: matches[0], heading };
  if (matches.length > 1) return { ambiguous: true, heading };
  const plainName = name.replace(/\.md$/, '').toLowerCase();
  const titled = notes.filter(note => note.title.toLowerCase() === plainName || note.path.split('/').at(-1)?.replace(/\.md$/, '').toLowerCase() === plainName);
  return titled.length === 1 ? { note: titled[0], heading } : { ambiguous: titled.length > 1, heading };
}
function scrollToHeading(heading: string) {
  const normalized = heading.replace(/^#+/, '').trim().toLowerCase();
  const elements = document.querySelectorAll('.note-article .markdown h1, .note-article .markdown h2, .note-article .markdown h3, .note-article .markdown h4');
  [...elements].find(element => element.textContent?.trim().toLowerCase() === normalized || element.textContent?.trim().toLowerCase().replace(/\s+/g, '-') === normalized)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
export function MarkdownView({ content, notes = [], currentPath, onOpen }: { content: string; notes?: NoteSummary[]; currentPath?: string; onOpen?: (path: string) => void }) {
  const [externalError, setExternalError] = useState('');
  return <div className="markdown">{externalError && <p className="form-error" role="alert">{externalError}</p>}<ReactMarkdown remarkPlugins={plugins} urlTransform={(url, key) => key === 'href' && url.startsWith('source://') ? url : defaultUrlTransform(url)} components={{
    a: ({ href, children }) => {
      if (href?.startsWith('source://')) return onOpen ? <button type="button" className="wiki-link" title="查看引用的源码" onClick={() => onOpen(href)}>{children}</button> : <span>{children}</span>;
      const wiki = href?.startsWith(WIKI_PREFIX);
      const markdownLink = href && !/^\w+:/.test(href) && (href.split('#')[0].endsWith('.md') || href.startsWith('#'));
      if ((wiki || markdownLink) && onOpen) {
        let target: string;
        try { target = decodeURIComponent(wiki ? href!.slice(WIKI_PREFIX.length) : href!); } catch { target = href || ''; }
        if (target.startsWith('source://')) return <button type="button" className="wiki-link" title="查看引用的源码" onClick={() => onOpen(target)}>{children}</button>;
        const { note, heading, ambiguous } = resolveNote(target, notes, currentPath);
        if (!note) return <span className="wiki-link unresolved" title={ambiguous ? `同名笔记不唯一，请使用完整路径：${target}` : `未找到笔记：${target}`}>{children}</span>;
        return <button className="wiki-link" title={`${note.path}${heading ? `#${heading}` : ''}`} onClick={() => {
          onOpen(note.path);
          if (heading) {
            if (note.path === currentPath) scrollToHeading(heading);
            else {
              let attempts = 0;
              const timer = setInterval(() => { attempts += 1; const article = document.querySelector('.note-article'); if (article?.getAttribute('data-note-path') === note.path) { scrollToHeading(heading); clearInterval(timer); } else if (attempts >= 30) clearInterval(timer); }, 100);
            }
          }
        }}>{children}</button>;
      }
      if (!href || !/^https?:\/\//i.test(href)) return <span title="此链接类型暂不支持打开">{children}</span>;
      return <a href={href} target="_blank" rel="noreferrer noopener" title="在浏览器中打开" onClick={event => {
        if (!window.businessmap) return;
        event.preventDefault(); setExternalError('');
        void window.businessmap.openExternal(href).catch(error => setExternalError(`链接打开失败：${errorMessage(error)}`));
      }}>{children}</a>;
    },
  }}>{stripFrontmatter(content)}</ReactMarkdown></div>;
}
