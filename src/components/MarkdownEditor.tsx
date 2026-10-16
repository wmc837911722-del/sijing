import CodeMirror, { EditorView } from '@uiw/react-codemirror';
import { markdown } from '@codemirror/lang-markdown';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { tags } from '@lezer/highlight';
import { useTheme } from '../theme';

const highlighting = syntaxHighlighting(HighlightStyle.define([
  { tag: tags.heading, color: 'var(--text)', fontWeight: '600' },
  { tag: tags.strong, color: 'var(--text)', fontWeight: '600' },
  { tag: tags.emphasis, fontStyle: 'italic' },
  { tag: [tags.link, tags.url], color: 'var(--accent)', textDecoration: 'underline' },
  { tag: [tags.keyword, tags.operator, tags.typeName], color: 'var(--text)', fontWeight: '500' },
  { tag: [tags.string, tags.number, tags.bool], color: 'var(--muted)' },
  { tag: [tags.comment, tags.meta, tags.processingInstruction], color: 'var(--faint)' },
  { tag: [tags.monospace, tags.quote], color: 'var(--muted)' },
  { tag: tags.invalid, color: 'var(--text)', textDecoration: 'underline wavy' },
]));
const extensions = [markdown(), highlighting];
const themeStyles = {
  '&': { backgroundColor: 'var(--bg)', color: 'var(--text)', height: '100%' },
  '.cm-content': { caretColor: 'var(--text)' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--text)' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection': { backgroundColor: 'var(--selection)' },
  '.cm-gutters': { backgroundColor: 'var(--panel)', color: 'var(--faint)', borderRight: '1px solid var(--line)' },
  '.cm-activeLine, .cm-activeLineGutter': { backgroundColor: 'var(--panel-2)' },
  '.cm-panels, .cm-tooltip': { backgroundColor: 'var(--surface)', color: 'var(--text)', border: '1px solid var(--line)' },
  '.cm-searchMatch': { backgroundColor: 'var(--accent-soft)', outline: '1px solid var(--line-strong)' },
  '.cm-searchMatch.cm-searchMatch-selected': { backgroundColor: 'var(--selection)' },
  '.cm-selectionMatch': { backgroundColor: 'var(--accent-soft)' },
  '.cm-foldPlaceholder': { backgroundColor: 'var(--surface)', color: 'var(--muted)', border: '1px solid var(--line)' },
  '.cm-button, .cm-textfield': { background: 'var(--input)', color: 'var(--text)', border: '1px solid var(--line)' },
  '.cm-tooltip-autocomplete > ul > li[aria-selected]': { background: 'var(--accent-soft)', color: 'var(--text)' },
};
const themes = { dark: EditorView.theme(themeStyles, { dark: true }), light: EditorView.theme(themeStyles, { dark: false }) };

export default function MarkdownEditor({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const theme = useTheme();
  return <div className="markdown-editor"><CodeMirror value={value} height="100%" theme={themes[theme]} extensions={extensions} onChange={onChange} basicSetup={{ lineNumbers: true, foldGutter: true, highlightActiveLine: true, syntaxHighlighting: false }} aria-label="Markdown 笔记编辑器"/></div>;
}
