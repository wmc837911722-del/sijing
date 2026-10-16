import { LoaderCircle, AlertCircle, Inbox, X } from 'lucide-react';
import { useEffect, useRef, type ReactNode } from 'react';
import type { QueryResult } from '../types';
import { cellValue } from '../api';
import '../ux-forms.css';

export function Busy({ text = '正在加载…' }: { text?: string }) {
  return <div className="busy" role="status"><LoaderCircle className="spin" size={18}/><span>{text}</span></div>;
}
export function Empty({ title, detail, children }: { title: string; detail?: string; children?: ReactNode }) {
  return <div className="empty-state"><div className="empty-symbol"><Inbox size={27}/></div><h3>{title}</h3>{detail && <p>{detail}</p>}{children}</div>;
}
export function ErrorBox({ error, retry }: { error: string; retry?: () => void }) {
  return <div className="error-box" role="alert"><AlertCircle size={17}/><span>{error}</span>{retry && <button className="text-button" onClick={retry}>重试</button>}</div>;
}
export function Modal({ title, onClose, children, wide = false, busy = false }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean; busy?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  const trigger = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => {
      dialog?.close();
      // React removes the dialog before passive cleanup; native close() alone
      // cannot reliably return focus to the button that opened it.
      queueMicrotask(() => {
        if (trigger.current?.isConnected && !document.querySelector('dialog[open]')) trigger.current.focus({ preventScroll: true });
      });
    };
  }, []);
  const close = () => { if (!busy) onClose(); };
  return <dialog ref={ref} className={`modal ${wide ? 'wide' : ''}`} onCancel={event => { event.preventDefault(); close(); }} onClick={e => { if (e.target === e.currentTarget) close(); }} aria-label={title} aria-busy={busy}>
    <div className="modal-surface"><header><h2>{title}</h2><button type="button" className="icon-button" title={busy ? '正在提交，请稍候' : '关闭'} aria-label="关闭对话框" disabled={busy} onClick={close}><X size={18}/></button></header>{children}</div>
  </dialog>;
}
export function ResultsTable({ result, compact = false }: { result: QueryResult; compact?: boolean }) {
  return <div className={`result-table-wrap ${compact ? 'compact' : ''}`}><table className="data-table"><thead><tr><th className="row-number">#</th>{result.columns.map((column, index) => <th key={`${column}-${index}`}>{column}</th>)}</tr></thead><tbody>{result.rows.map((row, index) => <tr key={index}><td className="row-number">{index + 1}</td>{row.map((value, j) => <td key={j} className={value === null ? 'null-value' : ''} title={cellValue(value)}>{cellValue(value)}</td>)}</tr>)}</tbody></table>{result.rows.length === 0 && <div className="table-empty">查询执行成功，没有匹配的数据。</div>}</div>;
}
