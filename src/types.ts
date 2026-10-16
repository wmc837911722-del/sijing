export type View = 'chat' | 'knowledge' | 'graph' | 'repositories' | 'query' | 'reports' | 'settings';
export type NoteSummary = { id: string; title: string; path: string; type: string; tags: string[]; updated_at: string; excerpt: string };
export type Note = NoteSummary & { content: string; revision: string; links: NoteSummary[]; backlinks: NoteSummary[] };
export type RepositoryConnection = { auth_type: 'none' | 'https' | 'ssh-agent' | 'ssh-key'; username?: string; proxy_url?: string; ca_bundle?: string; ssh_key_path?: string; known_hosts_path?: string; has_secret?: boolean; secret_storage?: 'keyring' | 'session' | 'none' };
export type RepositoryConnectionInput = Omit<RepositoryConnection, 'has_secret' | 'secret_storage'> & { secret?: string };
export type Repository = { id: string; name: string; url: string; path: string; branch: string; commit: string; status: string; last_sync?: string; auto_sync: boolean; ai_auto_sync?: boolean; connection?: RepositoryConnection; last_error?: { code: string; message: string } };
export type BusinessProject = { id: string; name: string; description: string; repository_ids: string[]; repository_roles: Record<string, 'frontend' | 'backend'>; revision: string; vault_path: string };
export type RepositoryModelPolicy = { provider_id?: string; provider_name?: string; kind?: string; base_url?: string; model?: string; status: 'unbound' | 'ready' | 'changed' | 'missing' };
export type AnalysisStatus = { status: 'never' | 'running' | 'failed' | 'completed'; summary: string; category?: string; updated_at?: string; commit?: string; scope?: { files: number | string[]; line_count: number; truncated: boolean; total_files?: number; total_modules?: number | string[]; repositories?: { repository_id: string; name?: string; commit?: string; files?: number | string[]; total_files?: number; line_count?: number }[] } };
export type FullAnalysisCoverage = { eligible_files: number; excluded_files: number; total_chunks: number; completed_chunks: number; analyzed_files: number; total_modules: number; completed_modules: number };
export type FullAnalysisStatus = { run_id?: string; status: 'never' | 'ready' | 'planning' | 'running' | 'paused' | 'failed' | 'blocked' | 'cancelled' | 'completed'; coverage?: FullAnalysisCoverage; modules?: { id: string; name: string; status: string; file_count: number; repository_ids: string[] }[]; summary?: string; error?: string; updated_at?: string; publication_complete?: boolean; stale?: boolean; conflicts?: { id: string; path: string }[] };
export type SourceEvidence = { repository_id: string; commit: string; path: string; blob: string; content: string; line_start: number; line_end: number; total_lines: number; truncated: boolean; next_line: number | null; reference: string; revision: string; untrusted: boolean; partial_line?: boolean };
export type Task = { id: string; status: 'queued' | 'running' | 'completed' | 'failed'; progress: number; message: string; error?: string; created_at: string };
export type Activity = { id: string; title: string; detail: string; kind: string; created_at: string };
export type Workspace = { name: string; vault_path: string; version: string; demo: boolean; stats: { notes: number; business_notes?: number; source_documents?: number; links: number; repositories: number; projects?: number }; repositories: Repository[]; projects: BusinessProject[]; recent_activity: Activity[] };
export type Graph = { nodes: { id: string; title: string; type: string; tags: string[] }[]; edges: { source: string; target: string }[] };
export type Provider = { id: string; name: string; kind: 'openai-compatible' | 'anthropic' | 'gemini'; base_url: string; model: string; has_key: boolean; secret_storage: 'keyring' | 'session' | 'none'; active: boolean };
export type Settings = { providers: Provider[]; active_provider_id: string | null; usage: { requests: number; input_tokens: number; output_tokens: number; cached_tokens: number | null }; mcp: { command: string; args: string[]; description: string } };
export type Datasource = { id: string; name: string; kind: 'sqlite' | 'postgresql' | 'mysql'; status: string; path?: string; host?: string; database?: string; username?: string; allowed_tables?: string[]; demo?: boolean };
export type QueryResult = { id: string; sql: string; columns: string[]; rows: unknown[][]; row_count: number; truncated: boolean; elapsed_ms: number; executed_at: string; datasource_id: string };
export type Report = { id: string; title: string; result: QueryResult; chart_type: 'bar' | 'line' | 'table'; created_at: string };
export type ChatResponse = { answer: string; sources: { path: string; title: string }[]; result?: QueryResult; provider?: string };
export type Notify = (message: string, kind?: 'success' | 'error' | 'info') => void;

declare global {
  interface Window {
    businessmap?: {
      request: <T = unknown>(method: string, path: string, body?: unknown) => Promise<T>;
      chooseDirectory: () => Promise<string | null>;
      chooseFile: () => Promise<string | null>;
      chooseRepositoryFile: (kind: 'ca' | 'ssh-key' | 'known-hosts') => Promise<string | null>;
      windowControl: (action: 'minimize' | 'maximize' | 'close') => void;
      openVault: () => Promise<void>;
      openExternal: (url: string) => Promise<void>;
      platform: string;
    };
  }
}
