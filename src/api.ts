export function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (!window.businessmap) return Promise.reject(new Error('本地服务尚未连接。请通过思径桌面应用打开工作台。'));
  return window.businessmap.request<T>(method, path, body);
}
export const get = <T,>(path: string) => request<T>('GET', path);
export const post = <T,>(path: string, body?: unknown) => request<T>('POST', path, body);
export function errorMessage(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/^Error invoking remote method 'businessmap:[a-z-]+': Error: /, '');
}
export function formatTime(value?: string) {
  if (!value) return '尚未同步';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date);
}
export function cellValue(value: unknown): string {
  if (value === null || value === undefined) return 'NULL';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
export function noteTypeLabel(type: string): string {
  return ({ business: '业务知识', overview: '业务概览', process: '业务流程', rule: '业务规则', entity: '业务对象', data: '数据口径', code: '内部资料', note: '知识笔记' } as Record<string, string>)[type] || type || '知识笔记';
}
