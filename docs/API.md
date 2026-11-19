# Desktop v0.3.0 implementation contract

All service endpoints except `/health` require `Authorization: Bearer <session token>` from the Electron main process. Bind to 127.0.0.1. Desktop API bridge: `window.businessmap.request(method, path, body?)`; responses are decoded JSON and errors throw a message. Preload also exposes `chooseDirectory()`, `chooseFile()`, `chooseRepositoryFile('ca'|'ssh-key'|'known-hosts')`, `windowControl('minimize'|'maximize'|'close')`, `openVault()`, `openExternal(url): Promise<void>`, `platform`. Repository file selection returns a path only, never private-key contents.

`openExternal` validates the renderer sender and delegates HTTP/HTTPS links to the operating system browser. URLs must have a hostname, contain no embedded credentials, and be at most 8,192 characters. File, executable, and custom-protocol links are rejected. Markdown reports launch failures inline; knowledge links continue to use the internal note navigator.

The service is started as `python -m businessmap --data-dir <path> --port <port>`, token from BUSINESSMAP_TOKEN environment. Frozen binary accepts the same flags. CWD must not determine data location.

## Knowledge/core (backend/core.py, backend/app.py)

- GET /api/workspace -> `{name,vault_path,version,demo,stats:{notes,business_notes,source_documents,links,repositories,projects},projects:BusinessProject[],repositories:Repository[],recent_activity:Activity[]}`
- GET /api/notes?q= -> `{notes:NoteSummary[]}`; summary `{id,title,path,type,tags:string[],updated_at,excerpt}`. id is vault-relative POSIX file path, includes .md.
- GET /api/note?path= -> `{...NoteSummary,content,revision,links:NoteSummary[],backlinks:NoteSummary[]}`. content includes original Markdown frontmatter.
- PUT /api/note body `{path,content,revision?}` -> same Note; existing file requires revision, mismatch -> 409.
- POST /api/notes body `{title,folder?}` -> Note.
- GET /api/graph -> `{nodes:{id,title,type,tags}[],edges:{source,target}[]}`. Raw/source documents and their edges are excluded from the business graph.
- POST /api/vault body `{path}` -> workspace (open/create only in user-chosen directory, refuse unsafe paths).
- POST /api/repositories body `{url,name?,branch?,connection?}` -> Repository. Local Git directory, HTTPS or SSH clone. No repository execution. Import runs in a task; connection testing is optional.
- POST /api/repositories/test body `{url,connection?,repository_id?}` -> `{ok:true,branches:string[],default_branch:string|null}`. Saved credentials may only be reused with their original repository ID and URL.
- PUT /api/repositories/{id}/connection body `{connection,branch?}` -> Repository. Updates credentials, network settings and optional branch for subsequent retries/synchronization; the repository URL remains fixed.
- POST /api/repositories/{id}/sync -> `{task_id}`.
- GET /api/tasks -> `{tasks:Task[]}`. Task `{id,status:'queued'|'running'|'completed'|'failed',progress,message,error?,created_at}`.
- GET /api/activity -> `{activities:Activity[]}`. Activity `{id,title,detail,kind,created_at}`.

Repository `{id,name,url,path,branch,commit,status,last_sync?,auto_sync:boolean,ai_auto_sync:boolean}`. `auto_sync` controls Git code-index synchronization; `ai_auto_sync` is independently derived from the persisted `ai_analysis:<id>.auto_enabled` setting and defaults to `false`. Read the current values through `GET /api/workspace` (`repositories`). The AI flag is consent for future automatic analysis, not a claim that the business map already matches the latest commit.

Repository also contains sanitized `connection` and optional `last_error:{code,message}`. Connection input is `{auth_type:'none'|'https'|'ssh-agent'|'ssh-key',username?,secret?,proxy_url?,ca_bundle?,ssh_key_path?,known_hosts_path?}`. Omitted/null `secret` preserves the existing credential when editing its repository; `''` clears it. Output excludes `secret` and adds `has_secret:boolean` and `secret_storage:'keyring'|'session'|'none'`. Credentials are kept in the OS keyring or session memory, not repository URLs, plaintext config, command arguments, logs or model context. Validation errors omit submitted values.

Git errors return `{detail,code}` with `network`, `authentication`, `permission`, `certificate`, `ssh_host_key`, `missing_credential`, `credential_cleanup`, `timeout`, `git`, or `invalid_configuration`. `credential_cleanup` (409) means the new settings/reference have been saved and the old credential is no longer used, but its OS-keyring deletion failed; unlock the keyring and save again to retry cleanup. Failed synchronization preserves published maps/indexes. HTTPS accepts username plus Token/password; SSH uses a local agent or an existing key with strict known-host verification. Encrypted keys must already be unlocked in the agent. Proxy and CA settings apply to HTTPS; no proxy credentials, browser SSO flow, arbitrary SSH commands or jump-host configuration are supported. Plain HTTP is limited to loopback test services. Local repositories observe committed HEAD without fetching or executing working-tree code.

Core owns `Store(data_dir)` with `.data_dir:Path`, `.vault:Path`, `.version`, `.config_get(key,default=None)`, `.config_set(key,value)`, `.config_update(key,updater,default=None)`, `.notes(query='') -> list`, `.read_note(path) -> dict`, `.save_note(path,content,revision=None) -> dict`, `.graph() -> dict`, `.workspace() -> dict`, `.activity(title,detail='',kind='info')`, `.repository_context(repo_id) -> dict` (bounded code context, not secrets). Store persists config with SQLite; `config_update` uses a `BEGIN IMMEDIATE` transaction for cross-process read/modify/write. Core must not import app. App calls tools router factory `create_router(store)` from businessmap.tools_api.

Tools register `store.on_repository_synced(repo_id,changed_paths)`. Core calls it after a changed Git commit and its code index are published. The callback queues an AI task only after the repository has opted into automatic AI analysis. Automatic task failures remain visible through `/api/tasks` and `/api/activity`.

Demo sample notes and SQLite database must be labeled as examples. Core creates `data_dir/demo.sqlite` containing orders(id,status,channel,amount,created_at), refunds(id,order_id,amount,status,created_at), seed deterministic rows. Repositories are not automatically sent to a model on import; /analyze invokes configured provider, UI labels action.

## AI, datasource, tools (backend/tools_api.py and supporting modules)

- GET /api/settings -> `{providers:Provider[],active_provider_id:string|null,usage:{requests,input_tokens,output_tokens,cached_tokens:number|null},mcp:{command,args,description}}`.
- POST /api/providers body `{id?,name,kind:'openai-compatible'|'anthropic'|'gemini'|'codex-local',base_url,model,api_key?}` -> sanitized Provider.
- POST /api/providers/{id}/activate -> `{ok:true}`.
- POST /api/providers/{id}/test -> `{ok,message}` (real request).
Provider `{id,name,kind,base_url,model,has_key,secret_storage:'keyring'|'session'|'none',active}`. Never return keys.
Local Codex uses `kind:'codex-local'`, `base_url:'codex://local'`, and an explicit model ID or `model:'default'`. It rejects API keys and delegates authentication to the installed official CLI's ChatGPT login. Its provider adds `codex_status:{installed,logged_in,status,version,detail}` and `capabilities:{complete:true,tool_turn:false}`. Status is cached briefly; the connection test performs an actual request. It supports text analysis/synthesis, not the application's tool-driven chat. Calls use isolated temporary working directories, disabled tools/hooks/plugins, deadlines and bounded output; no imported code is executed. The CLI does not expose a server-side output-token cap: prompts, response schema, measured usage and local size bounds enforce acceptance limits. Account plan limits still apply.
- GET /api/repository-model-policies -> `{policies:{[repository_id]:RepositoryModelPolicy}}`.
- GET /api/repositories/{id}/model-policy -> RepositoryModelPolicy.
- POST /api/repositories/{id}/model-policy body `{provider_id:string}` -> RepositoryModelPolicy. Binding itself sends no code and pauses automatic analysis. Explicit analysis or explicit re-enabling (after a prior successful analysis) can resume it.
RepositoryModelPolicy `{provider_id?,provider_name?,kind?,base_url?,model?,status:'unbound'|'ready'|'changed'|'missing'}` pins the provider ID and destination (kind, base URL and model); unbound repositories return only `{status:'unbound'}`. Name/API-key rotation does not change the destination. Manual/automatic analysis requires a valid binding and never inherits the active provider. Existing repositories require explicit binding too. Changing or deleting a destination blocks further requests until rebound.
- GET /api/datasources -> `{datasources:Datasource[]}`.
- POST /api/datasources body `{name,kind:'sqlite'|'postgresql'|'mysql',path?,host?,port?,database?,username?,password?,allowed_tables?:string[]}` -> Datasource sanitized.
- POST /api/datasources/{id}/test -> `{ok,message}`.
- GET /api/datasources/{id}/schema -> `{tables:{name,columns:{name,type}[]}[]}`.
Datasource `{id,name,kind,status,path?,host?,port?,database?,username?,allowed_tables?:string[],demo?:boolean,has_password:boolean,secret_storage:'keyring'|'session'|'none'}`. Omitted `allowed_tables` permits discovered regular tables; an explicit empty array permits none. Schema responses obey the same scope as query execution. Passwords are never returned.
- POST /api/query body `{datasource_id,sql}` -> QueryResult `{id,sql,columns:string[],rows:unknown[][],row_count,truncated,elapsed_ms,executed_at,datasource_id}`; readonly guards apply identically to all callers.
- GET /api/reports -> `{reports:Report[]}`; POST /api/reports body `{result_id,title?,chart_type?:'bar'|'line'|'table'}` -> Report `{id,title,result:QueryResult,chart_type,created_at}`.
- POST /api/chat body `{message,mode:'knowledge'|'data',provider_id?,repository_id?,datasource_id?,history?:{role:'user'|'assistant',content:string}[]}` -> `{answer,sources:{path,title,revision?}[],activities?:ToolActivity[],completion?:'complete'|'limited',result?:QueryResult,usage?,provider?:string|null}`. Legacy stateless endpoint remains available; client history is accepted for compatibility but is not forwarded because its model/repository authorization cannot be verified. Use persistent chats for conversational context. No configured provider: grounded excerpt search clearly marked; no simulated AI response. Knowledge mode never queries the business database. Data mode permits at most one query execution attempt after reading business evidence and the allowed schema; failed database execution is not automatically retried. Invalid discovery/tool arguments may be corrected within the run budget. If the model requests clarification, `result` is absent and no query executes. A non-demo datasource excludes explicitly labeled demo/example business notes from model evidence.
- GET /api/chats -> `{chats:ChatSummary[]}`, newest updated first, scoped to the current vault.
- POST /api/chats body `{title?:string|null}` -> empty `ChatSession`. An omitted/blank title becomes the first question's first 60 characters; an explicit title is preserved.
- GET /api/chats/{id} -> `ChatSession` from persistent server state.
- POST /api/chats/{id}/messages body `{message,mode:'knowledge'|'data',provider_id?:string|null,repository_id?:string|null,project_id?:string|null,datasource_id?:string|null}` -> updated `ChatSession`. Selection omission or `null` clears the prior selection for that request. The server supplies history; client-supplied history is rejected.
- POST /api/chats/{id}/cancel -> `ChatSession`. Marks a pending turn for cooperative cancellation; the current HTTP/model call may finish and be billed, but the next guard prevents further tools/SQL. It does not interrupt an already-running database query. Authentication and current-vault ownership are required.
- POST /api/repositories/{id}/analyze -> synchronous `{notes_created,notes_updated,summary,conflicts:{id,path}[],usage,provider}` for legacy standalone repositories. AI generates up to five business topics using privately validated source evidence, then publishes a business map and a separate analysis summary. Public notes contain business content without source positions. A repository assigned to a business project returns 409; start the project's joint frontend/backend analysis instead. Counts include all published documents; conflict candidates are not counted as saved knowledge. Missing/invalid model binding returns 409. A successful explicit standalone analysis enables future automatic AI updates for that repository. This call may wait for the model; the desktop bridge allows 180 seconds. Subsequent automatic analyses run as background tasks using the same binding.
- GET /api/analysis-status -> `{statuses:{[repository_id]:{status,category?,summary?,updated_at,commit?,scope?}}}` for current-vault repositories. Sanitized running/completed/failed status survives reloads; no raw provider response is returned.
- GET /api/source-evidence?reference= -> 403. The public business workspace cannot retrieve source code. Source reading remains internal to authorized fixed-snapshot analysis.
- POST /api/repositories/{id}/auto-analysis body `{enabled:boolean}` -> `{ok:true,enabled:boolean}`. Enabling requires a prior successful explicit analysis and a valid bound provider (otherwise 409). Pausing prevents queued/future automatic model requests; it does not interrupt a request already sent or disable Git code-index synchronization. Refresh `/api/workspace` after changing the switch.
- GET /api/analysis/candidates -> `{candidates:AnalysisCandidate[]}` for the current vault only.
- POST /api/analysis/candidates/{candidate_id}/apply body `{revision:string|null}` -> Note. This explicitly replaces the target note with the candidate content, guarded by the current note revision; it is not an automatic text merge. Missing candidate returns 404; a newer source commit or mismatched note revision returns 409. Fetch `/api/note?path=...` when reviewing, and send that current revision after the user elects to apply. `null` is only appropriate when the target does not exist. Successful application removes the candidate and marks the accepted note current.

AnalysisCandidate `{id,key,path,content,current_revision:string|null,base_revision?:string|null}`. Source ownership, commits and source paths remain private and are rechecked on apply. `current_revision` records the revision seen during generation and may already be stale. Candidates are stored outside the Markdown vault/index, never substituted for human-edited notes or included in knowledge retrieval before application. Generated notes with unresolved conflicts or an outdated source commit are excluded from AI knowledge retrieval; knowledge navigation and MCP also exclude these stale/conflicted notes; a direct read returns 404 rather than usable evidence.

Analysis inputs include short, exact source excerpts with request-scoped `evidence_id` values, counted within the serialized context budget. A model may cite those IDs without retyping code; unknown IDs and contradictory coordinates are rejected before publication. Legacy free-form quotes still require an exact, uniquely located match in the supplied source. Truncated source fragments are cut at complete line boundaries before evidence IDs are assigned; an untruncated file without a final newline retains its complete final line. Original text, coordinates and source ownership remain private analysis evidence; public Markdown contains business prose only. Publication validates titles, paths, metadata and body before saving. Provenance validation does not certify every semantic claim. Citation failures persist a controlled reason in the status summary and safe local diagnostics, without saving raw model output or automatically retrying a paid request.

ChatSession `{id,title,created_at,updated_at,messages:ChatMessage[],mode,provider_id:string|null,repository_id:string|null,project_id:string|null,datasource_id:string|null,pending:boolean,pending_activity:ToolActivity[],cancel_requested:boolean,earlier_messages_removed:number}`. ChatMessage `{id,role:'user'|'assistant',content,sources?:{path,title,revision?}[],activities?:ToolActivity[],completion?:'complete'|'limited',result?:QueryResult,provider?:string|null,usage?,error?:string}`. ChatSummary contains the same identifiers, title, timestamps, selections and `pending`, plus `message_count` (retained messages) and `preview` (last content, up to 120 characters), without the message bodies. The session retains the latest 200 messages; `earlier_messages_removed` reports how many were removed. Query results are actual saved receipts and can be passed to the report API by their `id`.

Sessions and summaries persist in the local SQLite configuration database across backend restarts. They belong to their creation vault; switching vaults hides them until that vault is reopened. Each reply uses at most the last eight contiguous messages (2,000 characters each) with the same mode, repository, data-mode datasource, model destination and repository authorization scope. Changing those boundaries or a failed reply starts a new model context; older messages remain visible locally but are not forwarded across boundaries. `provider_id:null` uses the currently active provider subject to repository authorization; selecting a provider affects only that request and never changes the global active provider.

Repository selection is enforced by every navigation tool using private document ownership and current source versions. Pending proposals, technical source indexes and historical version notes are excluded. `repository_id:null` searches eligible notes throughout the current vault. Unknown provider/repository/datasource/chat IDs return 404 before appending a message. A repository from another vault is also invalid. These selections do not change database authorization: datasource permissions and the SQL guards remain authoritative.

Internal model requests enforce repository bindings for knowledge and data-mode evidence. A scoped request with a mismatched model is rejected; full-vault retrieval filters unauthorized repository material, including moved notes retaining private ownership by revision. Model target and authorization are rechecked before dispatch. This is not global DLP: manual notes without source ownership, the user's question, database content and external MCP clients are separate authorization boundaries; public business prose validation still applies to knowledge documents. Local model-free reading remains available. Users must independently authorize the destination used by an external MCP client.

The user message is persisted before calling a model. A provider, validation, or query failure after acceptance returns HTTP 200 with the original question and a readable assistant `error`; unknown internal errors are sanitized. Concurrent sends to one chat return 409 without appending; other chats can progress independently. Kernel file locks enforce this across backend instances. A reload after a crashed request retains the question and records an interrupted-reply error once the lock is released. `pending` exposes an in-progress reply. As with all `/api` routes, authentication is required; malformed payloads return 422.

Automatic AI analysis is serialized, deduplicated per pending repository, and prioritizes changed paths plus existing source dependencies. It performs bounded reanalysis rather than claiming complete semantic impact propagation. A changed commit during analysis invalidates the result; automatic work has at most one additional attempt. Analysis scope limits and conflicts are reported in summaries/activity, and failed tasks expose an error. Generated business notes use project/topic names under `01-Wiki`, maps under `02-Areas/业务地图`, and summaries under `03-Projects`. Source indexes live in private metadata and never create public source Markdown. Public frontmatter contains only title, type, content_role, tags and generated:true. Private path/revision ownership retains source versions, authorization and human-edit guards. Removing technical frontmatter cannot make generated notes unowned.

Per-call usage includes actual `input_tokens`, `output_tokens`, `cached_tokens` (unknown values are `null`), with provider-specific `cache_creation_input_tokens`, `cache_read_input_tokens`, or `cache_miss_tokens` when applicable. OpenAI-compatible adapters recognize both `prompt_tokens_details.cached_tokens` and DeepSeek-style `prompt_cache_hit_tokens`. Anthropic stable system/evidence blocks request ephemeral caching; actual availability and hits depend on the provider. No fixed hit rate is promised.

Since 0.2.9, provider presets are editable UI templates; they introduce no provider-ID whitelist or saved-schema migration. The user supplies a model available to their account. Twelve domestic presets use the existing synchronous Chat Completions adapter. See [platform and credential reference](MODEL-PROVIDERS.md); preset presence does not certify live account access or streaming-only models. No universal temperature, thinking-mode or JSON-mode parameter is added. The official `api.deepseek.com` host explicitly sets `thinking:{type:"disabled"}` for bounded analysis/tool responses; gateways do not inherit this by model-name matching. Official MiniMax hosts request `reasoning_split:true`; the official Spark host on `/v1`, `/v2`, and `/x2` explicitly disables its built-in `web_search` tool. These vendor parameters are not inferred from a gateway's model name or URL substring.

`POST /api/providers/{id}/test` retains a 32-token output budget. A well-formed Chat Completions response with empty final content, nonempty string `reasoning_content` and `finish_reason:length` confirms connectivity only: `{ok:true,message}` explicitly says a complete answer was not verified. Normal business requests reject the same response, never expose its reasoning or execute SQL, and account for its reported usage. Other empty/malformed responses remain errors. Testing does not raise output budgets automatically or retry paid calls.

MCP stdio uses the same local data directory, vault, table scopes, SQL guards and database read-only protection. No model configuration is needed by the external AI. The settings endpoint returns a CWD-independent launch command: development uses the absolute `backend/entry.py` path with `--mcp --data-dir <path>`; the frozen sidecar uses `--mcp --data-dir <path>` directly. `python -m businessmap.mcp_server --data-dir <path>` also works when the package is installed or on `PYTHONPATH`. Session-only database passwords do not cross into a separate MCP process; OS keyring credentials do.

MCP tools: `knowledge_list(folder='',offset=0,limit=30)`, `knowledge_search(query,limit=8,offset=0)`, `knowledge_outline(path,offset=0,limit=100)`, `knowledge_read(path,offset=0,limit=16000,heading?,revision?)`, `knowledge_links(path,direction='both',limit=30,offset=0)`, `knowledge_graph(path?)`, `datasources()`, `datasource_schema(datasource_id)`, `query_readonly(datasource_id,sql)`. Read's original first three positional parameters are retained. No tool modifies Markdown or business database state. Queries record local result receipts and activity; database results can change. Reads return content/revision, selection range, relative offset, next_offset, total_chars, truncated and is_current. Passing an old revision returns 409. Navigation excludes stale/conflicted managed notes; graph output is capped at 500 nodes. Internal AI additionally applies repository/model scope and excludes demo notes for real datasources. External MCP clients remain a separate local-read authorization boundary.

## Progressive knowledge exploration (0.3.0)

Internal chat uses native tool calling for OpenAI-compatible, Anthropic and Gemini adapters. Each model response chooses the next navigation operation. There is no fixed initial evidence batch or mandatory root traversal. Search returns short match context; outline/read/links progressively supply headings, selected text and directed edges. Wiki links, local Markdown links and backlinks resolve conservatively: missing, ambiguous or inaccessible targets do not become edges. Markdown remains authoritative, and externally edited/renamed files refresh the local metadata cache.

`knowledge_search` returns total/offset/truncated/next_offset over eligible business documents. Business chat never registers `repository_search` or `repository_read`; attempts to request these tools cannot reveal Git text. Answers, sources, activities, model-free excerpts and persisted history apply the same public business-content boundary. Data queries and saved SQL reports retain their separate read-only database protections. No imported code is executed.

`finish_answer(answer,citations,kind)` ends a knowledge answer; citations must refer to nonempty note bodies or exact source ranges read in this turn. Source indexes alone cannot establish a business answer. A clarification may have no citations. UI sources are generated from actual read receipts, never invented by model output. This verifies source provenance, not the semantic truth of every model sentence. In data mode, `datasource_schema()` refers only to the selected datasource and `query_readonly(sql,evidence_paths)` requires both that schema and read evidence. Results are rendered directly from the query receipt and are not sent to the model. Knowledge mode has no database tools.

`ToolActivity` is `{id,tool,label,status:'running'|'complete'|'error',path?,detail?}`. Persistent sessions save bounded public activity receipts; GET chat polling exposes in-flight progress. Completion, errors and crash recovery retain that record. Raw prompts, private reasoning/signatures and retrieved contents are not activity fields. Conversation text and evidence history are still local sensitive data, subject to the existing vault boundary.

A run permits at most 10 model turns, 18 tool operations, one database execution attempt and 60,000 reported aggregate input/output tokens. Reported usage is checked between requests; preflight additionally reserves space for a final synthesis. When the preceding provider reports reliable input usage and the request keeps its identical system/tools and message prefix, it estimates the next input from that actual count plus conservative UTF-8 bytes for appended content. Unknown usage or changed context falls back to the complete request byte size. Preflight is not reported as actual token usage. Unknown usage remains unknown. Internal reads cap each page at 6,000 characters, transcripts stop growing at 38,000 characters, and adapters reject total system/tool/transcript context above 48,000 characters without silently dropping evidence. Duplicate operations return a repairable tool error; two plain-text replies without the required tool protocol stop with a model-compatibility message. Before a hard stop, a bounded final call exposes only `finish_answer` and whole already-read evidence chunks, with no new reads or SQL. If any read evidence or question is omitted to fit, only clarification is accepted; a full factual conclusion is not claimed. Model/network failures are not automatically retried. Budget stops return `completion:'limited'`; timeout/cancellation/policy errors use normal chat error handling.

The 150-second run deadline is checked before/after each operation and each model dispatch; model HTTP timeouts are bounded by the remaining budget (up to 60 seconds per call). Cancellation is cooperative, not an HTTP abort. A slow ongoing operation must return before its post-call guard runs. Before further model transmission, tools or final answers, the runtime checks vault, model destination, repository policies, datasource configuration, and every encountered note revision/freshness. Changes end the run rather than mixing old and new evidence. It does not persist/resume partially completed model transcripts after a crash.

During ordinary exploration the system prompt and ordered tool schemas remain stable within a mode and results append in order, improving eligibility for provider prefix caches. The reserved final-synthesis call uses a separate bounded context and finish-only schema. Anthropic requests ephemeral caching for stable instructions and the most recent user/tool-result block. Exact hits depend on service/model thresholds and provider behavior. Per-turn usage aggregates all model calls; missing cache figures remain null. No final business-answer cache or database-result cache is used.

Design references: [Obsidian CLI](https://obsidian.md/help/cli), [Vault API](https://docs.obsidian.md/Plugins/Vault), [Internal links](https://obsidian.md/help/links), [Copilot agent tools](https://docs.obsidiancopilot.com/agent-mode-and-tools/). This is Sijing's own implementation inspired by Markdown vault navigation; it is not based on Obsidian source code or dependent on an Obsidian installation.

## Shared UI behavior

Since desktop version 0.2.8, the product is displayed as 思径 · Sijing and the MCP server advertises `Sijing`. Existing application identifiers, the `businessmap` URL/IPC origin and Python package, `BUSINESSMAP_*` environment variables, data paths, keyring service names and MCP tool names remain unchanged. The rename requires no configuration or vault migration. Updated system-prompt branding changes the provider prompt prefix; previously cached prefixes may no longer match.

The desktop uses system fonts and shared neutral light/dark surface tokens. The business-map inspector defaults to open at viewport widths of 1280 CSS pixels or more and closed below that threshold. An explicit toggle overrides the responsive default for the current window session; resizing then retains that choice. Long native forms keep their title and close action visible while scrolling.

Graph layout runs in a local Worker under `worker-src 'self'`, without access to desktop IPC or repository execution. Graph data and Markdown remain authoritative; window-local viewport/layout snapshots are keyed by vault and graph scope and capped at 12 entries. Restoring a settled snapshot initializes the simulation at rest, so the next drag does not restart a full initial layout. Layout updates carry a generation and drag revision; stale responses cannot overwrite the pointer-controlled node. Graph filtering, theme changes, and resizing preserve positions and viewport. WebGL2 rendering has a Canvas fallback; labels use a separate Canvas so overview text can fade by zoom scale. Reduced-motion preference also applies to canvas interactions and simulation updates.

New-chat drafts are local until first send; repeated new-chat clicks reuse the unsent draft. Chat input is limited to 8,000 Unicode code points, retaining over-limit text while disabling submission. SQL drafts and results are keyed by datasource and remain mounted across page navigation; late results update only their originating datasource. Unsaved chat and SQL drafts are window-local and are not promised to survive app exit.

Note saves submit a fixed content snapshot with its revision. A successful response updates the saved revision without replacing edits typed during the request. Submitting forms expose their busy state and block dismissal until completion; failures stay visible inside the form and retain its values.

Use the above envelope keys exactly. Renderer handles loading/empty/error states, no fake successes. API validation/authorization failures return a non-success HTTP status with `detail`; accepted persisted-chat reply failures are represented by the assistant message's `error`. Changes outside the app must eventually appear. Repository import alone never sends source to a model; explicit AI analysis opts the repository into subsequent automatic updates, which can be paused independently. Windows/macOS/Linux builds, only native-host tested platforms may be called verified.

Onboarding is UI-only: the native modal highlights real controls and navigates existing views without submitting forms, testing providers, analyzing repositories or executing SQL. The versioned localStorage key `businessmap.onboarding.v1` stores only `{status: "paused" | "done", step: number}`. Dismissed guidance does not auto-open again; the sidebar entry resumes the last step or restarts a completed tour. Invalid preferences fall back to the introduction. Navigation preserves the existing unsaved-note guard.


## Business projects and paired import

A `BusinessProject` is `{id,name,description,repository_ids:string[],repository_roles:{[repository_id]:'frontend'|'backend'},revision,vault_path}`. It owns business outputs; each member remains an independent Git source with its own connection, branch and commit. Records are scoped to the current vault. A repository belongs to at most one project.

- GET /api/projects -> `{projects:BusinessProject[]}`.
- POST /api/projects body `{name,description?,repository_ids,repository_roles?}` -> BusinessProject. Groups already imported repositories without model calls.
- POST /api/projects/import body `{name,description?,frontend:RepositoryInput[],backend:RepositoryInput[]}` -> BusinessProject. Both sides need at least one repository. Each input contains `{url,name?,branch?,connection?}` using the existing repository connection contract. All inputs are validated before import; code cloning/indexing remains asynchronous. A failed Git sync remains visible and retryable within the project; no code is executed or sent to a model during import.
- PUT /api/projects/{id} body `{revision,name?,description?,repository_ids?,repository_roles?}` -> BusinessProject. Revision mismatch returns 409; duplicates, cross-vault members and repositories owned by another project are rejected.
- GET /api/projects/{id}/model-policy -> RepositoryModelPolicy-compatible aggregate status. All member destinations must agree.
- POST /api/projects/{id}/model-policy body `{provider_id}` explicitly binds every member to that provider destination. It does not call a model or enable automatic analysis.
- GET /api/projects/{id}/analysis-status -> sanitized AnalysisStatus with per-repository coverage when available.
- POST /api/projects/{id}/analyze -> `{task_id,summary}`. Schedules or resumes complete project analysis: local full-source planning, bounded complete-line model batches, then business-domain synthesis and publication. Successful batches for the same snapshot are reused. Grouped repositories reject standalone map generation and automatic single-repository AI updates with 409. Project analysis remains explicit.
- GET /api/projects/{id}/full-analysis -> `{status,run_id?,snapshot_id?,coverage?,modules?,summary?,error?,exclusions?,publication_complete?,stale?,interrupted?,reused_chunks?,receipt_migration?,synthesis_failure?}`. Reading progress never starts a model. Coverage contains `eligible_files,excluded_files,analyzed_files,total_chunks,completed_chunks,total_modules,completed_modules,total_lines,analyzed_lines`. An interrupted ownerless run can resume; a historical sampled preview cannot establish full completion. Optional `synthesis_failure` is either `{codex_reason,status_code?}` for an allowlisted Codex cause, or `{status_code:422,reason_code,fact_position?,fact_kind?,public_content_kind?}` for a controlled public-projection validation failure. A newly observed public-text rejection may identify its rule family in `public_content_kind`. No matching text or token is stored. Historical failures without a category retain the generic message. Validation reasons come from a server allowlist; position is a bounded zero-based global fact integer and kind is `context`, `finding` or `unknown`. Fixed business-friendly text appears in `error`; raw model/transport text, source paths and credentials are never returned. The diagnostic clears on retry/success or pause/cancel.
- POST /api/projects/{id}/full-analysis/pause body `{paused:boolean}` pauses new dispatch or explicitly resumes the queued job. In-flight calls may finish and save receipts; successful batches are retained, failed calls are not automatically retried.
- POST /api/projects/{id}/full-analysis/continue-with-model body `{provider_id,source_run_id}` explicitly changes the bound destination and creates a paused continuation after verifying the still-current old snapshot, fixed/redacted input hashes, line segments and citations. It sends no model request and leaves the old run unchanged. The new run reports `reused_chunks` and `receipt_migration:{source_run_id,source_provider,target_provider,source_provider_name,target_provider_name,created_at}`; inherited chunks retain their original provider and receipts. This is separate from ordinary rebinding, which never automatically imports another destination's results. Resume with the existing pause endpoint. Mixed-provider origins remain in private receipts and are not added to public documents.

Full project output uses human-readable project/category/topic paths: `01-Wiki/<project>/<实体|流程|规则|数据口径>/<topic>.md`, `02-Areas/业务地图/<project>/业务地图.md`, `03-Projects/业务梳理/<project>/分析总结.md`. Oversized documents retain complete business content in linked parts. Source/module/run IDs, commits, paths and raw receipts remain internal. Private semantic coverage records a decision for every original input, including public support, retained uncertainty, evidence-backed resolution, technical material and local source-scope observations. Mixed unknowns require separate treatment of their questions; static evidence cannot resolve untested runtime behavior. Business-operation candidates require bounded generation and independent semantic review before publication. Privacy, coverage or semantic failures preserve checkpoints and block publication; rejected review does not automatically repeat paid requests. Targeted source supplements have separate receipts and never replace successful main batches. Coverage distinguishes eligible files, completed batches, and exclusions; source completeness requires every included line interval. Source coverage and disposition counts alone certify neither business-semantic completeness nor runtime correctness.

Each semantic packet has at most two drafts. For trusted local Codex only, a mechanically valid first draft followed by a complete rejected review supplies the second draft with the full original packet, a lossless prior-candidate table, exact negative checks and a server-derived preservation scope. Unrelated claims and private dispositions must remain unchanged; the guard rejects drift rather than repairing a response. Shared checkpoint and publication readers authenticate this same context before deriving the original packet. The independent second review receives original facts and the new candidate, without prior opinions. The complete revision request still has the existing byte limit; an oversized context sends no paid second draft. HTTP and mechanically invalid first drafts retain their existing behavior. Transport, privacy, unknown-result and second-draft failures remain terminal; there is no third draft or automatic replay of an exhausted attempt.

Chat and persistent chat message bodies accept optional `project_id`; it is mutually exclusive with `repository_id`. Session selections and summaries retain the project ID. Project chat reads eligible business documents only. Private analysis receipts retain actual repository/commit/path/range, including when two members have identically named files; source tools are not registered for ordinary chat. All member policies, commits and the project revision participate in history and in-flight guards. Changing a member not yet read also invalidates the old run. Project documents must match current managed revision and all member commits to serve as current evidence. Exact source checks establish provenance; business semantics still require review. See [business-project decisions](BUSINESS-PROJECTS.md).

### 完整分析的私有 L0 导航

完整分析内部采用 L0 routing v8；没有新增公开端点。HTTP 与 Codex 的 L0 响应均仅接受唯一 fact_id 及 public 的 category/title 或 internal 的 classification/reason，拒绝 text、kind 等多余字段。导航标签不决定最终发布或事实删除，完整原始事实继续参与 schema8 的综合、独立评审及支持证明。

缓存采用精确身份校验：v8 结果完整重验；旧 v7 已接受结果经冻结协议重验并核对归一化一致性后，只读提取导航，不重写旧行或伪造新成功。旧分批边界保留；同时存在的两版本必须具有相同消费导航。当前 semantic_input_hash 已有任意任务时，所有批次必须在任何模型调度前命中有效缓存；缺失、损坏或冲突以受控 cache_invalid 停止。SOURCE1、semantic_input_hash 公式、后续 semantic HTTP 契约与公开隐私门禁保持不变，没有自动重试或第三稿。缓存成功提交的私有结构化事件在事务提交后记录，命中事件不写回缓存。

### 私有目录 transport 恢复控制（非 HTTP API）

`businessmap.semantic_recovery.catalogue_recovery_plan(snapshot, run, parent_stage_hash, *, read_checkpoint)` 只读生成一个精确计划；`run` 是从当前完整任务、源码批次及已接受补充重建的 context，必须含 `semantic_input_hash`。它不构造 Store、写库、解暂停或调用模型。

`authorize_catalogue_recovery(controller, snapshot, run, plan, *, authorization)` 的 `controller` 是 `FullProjectAnalysis` 实例。函数内部取得项目 OS 排他锁，核对暂停/未取消、当前快照，并从当前 SQLite run/chunks/addenda/matching 重新计算输入摘要；同一事务只创建一份 `business_semantic_recoveries` 回执和一个 planned child。重复同一计划/同一授权幂等返回；不解暂停、不发送。普通 resume 不创建此授权。

`authorization` 必须是绝对路径、regular、非 symlink、权限 `0600` 的 JSON 文件。封闭字段：`policy, plan_hash, parent_stage_hash, run_id, operator_id, authorization_source, authorized_at, expires_at, max_uses, reconciliation_ref, reconciliation_hash`。固定 `operator_id=root`、`authorization_source=user-fix-and-continue`、`max_uses=1`；时间为严格整数，窗口恰好 3600 秒。`reconciliation_ref` 指向原始字节 SHA-256 等于 `reconciliation_hash` 的本地 JSON，封闭字段为 `checked_at, local_owner_pids, local_processes_drained, upstream_result_or_billing_reconciled`。要求 60 秒内、非空唯一正整数 PID 全部不存在、两个布尔值分别为 true/false。operator 还需实际确认没有遗留 CLI 进程；这不证明上游未计算或未计费。

`semantic_checkpoint_reader(db)` 返回保持普通调用签名的可信读取对象，并提供 `.raw_digest(stage_hash)`、`.recovery_receipts(input_hash)`。读取会在尚无事务时开启只读事务，不创建表；调用方负责连接和事务生命周期。`semantic_checkpoint_rows(db,input_hash)` 返回明确的 `SemanticCheckpointRows(list)`，其 `.read_checkpoint` 冻结同一事务中的全部任务、原始行摘要及回执，连接关闭后仍可验证。转换为普通 list/dict 会丢失可信恢复能力，恢复证明应拒绝；普通无恢复证明仍兼容旧 callback。

`authenticate_recovery(row, *, snapshot, expected_input_hash, read_checkpoint)` 接收解码 checkpoint（stage_hash/stage/input_hash/status/attempt/data），返回通过当前真实 DB 祖先认证的完整回执。`receipt_hash(receipt)` 只摘要不可变授权字段。投影中的 `verification.recovery_receipt_hashes` 只引用这些摘要，不能作为授权来源。

### Catalogue merge contract and operator supersession

`bounded-operation-navigation-v1` is a stage-specific catalogue merge contract.
Leaf/subdivide requests and base semantic input/generator identities retain their
existing bytes. `semantic_stage_system(snapshot, stage, request=None)` selects the
new fixed system only for a catalogue merge carrying the explicit marker; request
preview, dispatch and identity use that same request-aware selection.

Each new merge item contains exactly `label_id`, `title`, `category`, `fact_count`,
`alias_count`, and `alias_digest`. Complete private aliases and fact membership
remain in normalized results and accepted parent operations. This is compressed
navigation, not a promise of lossless semantic interpretation. The 128-item and
64,000-byte bounds and full-text proposal/review remain applicable.

The operator-only `semantic_supersession` module exposes
`catalogue_supersession_plan`, `authorize_catalogue_supersession`,
`authenticate_supersession`, `supersession_inventory`,
`supersession_receipt_hash`, and `consume_supersession`. Its distinct policy is
`operator-catalogue-merge-contract-once-v1`; historical leaf recovery v1 remains
unchanged. `_check_grant(authorization, plan)` is pure file preflight and must run
before any operator helper constructs a Store. Authorization requires a specific
root grant, 600-permission evidence and reconciliation files, a 3,600-second
window and strictly one use. Authorization leaves the run paused and atomically
creates one planned child plus an immutable receipt. Consumption uses SQL CAS;
an unknown running child is not resent. Old rows and paid occupancy remain.

A trusted checkpoint reader additionally exposes `task_rows(input_hash)`,
`supersession_receipts(input_hash)`, `catalogue_source(run_id)`, and
`catalogue_runs(project_id)` alongside
`raw_digest` and `recovery_receipts`. The source bundle contains persisted `run`,
position-ordered `chunks`, ID-ordered `addenda` and `matching_receipts`, and
cache-key-ordered `l0_rows`. Frozen readers deep-copy returned data after their
SQLite read transaction closes. `reconstruct_catalogue_frontier` replays original
L0 batch/response order and exact old page wire, returning `frontier`, `bindings`,
`catalogue_task_hashes`, `failed_pages`, and `completed_root`; it never calls a
provider or fills missing legacy pages.

Only proofs with an actual supersession receipt add
`verification.supersession_receipt_hashes`, an exact sorted receipt-hash list
included in the projection hash. Existing proofs without supersession retain
their prior digest. Inline proof assertions cannot replace the trusted database
receipt and original failed-parent closure.

The private `catalogue_runs(project_id)` seam returns deep-copied persisted run
dictionaries scoped to that project. SQL run/project/snapshot identifiers must
match decoded fields. Frozen readers retain these records in the same SQLite
snapshot as tasks and source data. For a mixed legacy/bounded proof, product
readers recompute source input identity and require exactly one matching run;
no projected run, latest-row ordering, or receipt substitutes for this lookup.
All adopted catalogue pages must close over the final root, and each parent
operation may be consumed only once. Current, stored, and support readers reject
extra accepted pages even when their individual hashes and results are valid.

Ordinary bounded catalogue merge transport failures may use the separate
`operator-bounded-catalogue-provider502-once-v1` policy through the existing
recovery plan/authorize API. The parent must be an attempt1 needs_review
catalogue merge with the exact bounded marker, model_call/provider_error,
integer502, null result and no recovery/supersession metadata. The policy is
selected from the authenticated parent; callers cannot select a broader retry
mode. It preserves the exact request/context/system and logical task key and
uses the existing recovery child hash and unchanged closed receipt schema.
The original `operator-catalogue-provider502-once-v1` leaf policy is unchanged.

Mixed proofs retain every failed parent and its paid occupancy, all recovery
receipt hashes, and the independent supersession hashes. The active catalogue
DAG uses the accepted physical recovery child and its downstream references;
only its authenticated failed logical parent is excluded from accepted-page
traversal. Legacy replay authenticates relevant v1 leaf recoveries locally to
avoid recursion, while complete inventory authenticates every policy. Default
operator planning remains read-only; invalid grants reject before Store, and
authorization leaves the run paused without dispatch.

### Interrupted ordinary bounded catalogue reconciliation (private)

`operator-bounded-catalogue-interrupted-once-v1` is an additive operator policy
for an authenticated ordinary bounded catalogue merge left running at strict
attempt 1, with null result, null raw SQL error, and no diagnostic/failure or
one-shot metadata. It preserves the original raw running row and paid occupancy;
it appends one distinct transport child through the existing plan, authorization,
receipt and consumption APIs. Normal wire, input, generator and closed
PLAN/AUTH/RECEIPT fields are unchanged. Other unknown running tasks still block.

The trusted live and frozen checkpoint readers additionally expose
`raw_error_code(stage_hash)`: exact SQL error value, with missing rows rejected.
The frozen accessor captures the same transaction as raw digests and receipts.
Only the interrupted policy requires this seam; older callback readers retain
older-policy compatibility.

Only this policy uses the closed eleven-field reconciliation document:
`checked_at`, `local_owner_pids`, `local_processes_drained`,
`upstream_result_or_billing_reconciled`, `run_id`, `parent_stage_hash`,
`parent_row_digest`, `dispatch_evidence_ref`, `dispatch_evidence_hash`,
`result_recovered`, `duplicate_call_risk_accepted`. It requires a check within
60 seconds before authorization (and current age at most 60 seconds for new
authorization preflight), unique positive drained owner PIDs, no recovered result,
and explicit
acceptance of possible duplicate upstream work. This does not establish zero
upstream work or reconciled billing.

The hash-bound dispatch document has exactly eight fields: `run_id`,
`input_hash`, `parent_stage_hash`, `parent_row_digest`, `events_ref`, `events_hash`,
`reserved_event_index`, `started_event_index`. The private JSONL snapshot must
end with adjacent exact-parent/input catalogue reservation and started records;
strict attempt 1 and finite ordered numeric `at` timestamps are checked. Any
later record rejects the narrow path. Grant, reconciliation, dispatch and event
files are opened through one no-follow descriptor each and must be mode 0600
regular files. Old four-field reconciliation and old event payloads are unchanged.

New grant preflight checks current evidence freshness before Store construction.
Later planned/unconsumed `authenticate_recovery` and consumption require the
immutable check to have been within 60 seconds before the original `authorized_at`
and current time to remain inside that exact 3600-second grant. Cached startup
may exceed 60 seconds without rewriting evidence or extending expiry. They still
recheck current PID absence and all immutable hashes/events; authorization and
consumption retain the existing current-context controls and transaction, and
consumption uses the existing one-shot CAS. Historical consumed receipts authenticate without reopening expired or
removed evidence files. Received replay is free; an unknown consumed child may
never recover or resend. New-policy recovery events add `policy` after commit.

### Complete paid operation frontier (private proof)

`businessmap.semantic_catalogue_frontier.catalogue_frontier_plan(snapshot,
expected_input_hash, *, read_checkpoint, inputs=None)` is a pure read-only
plan. Trusted SOURCE/L0 must supply the canonical full fact order through
exactly one input-authenticated persisted run and both trusted source callbacks.
Missing, empty or mismatched source metadata rejects; supplied inputs only
cross-check that source and never replace its coverage or ordering authority. The
returned object contains operations, bindings, page_hashes, received_to_accept,
terminal_candidate, recovery_receipt_hashes, supersession_receipt_hashes and
occupied_calls. A preview containing received results is not a final proof.

The additive `verification.catalogue_terminal` contract
`complete-operation-frontier-v1` has exactly seven fields: contract, input_hash,
generator_hash, page_hashes, frontier, operations_hash and terminal_hash.
page_hashes is the sorted complete set of adopted physical catalogue pages.
frontier contains closed stage_hash/result_hash/operation_id bindings parallel
to the complete operation list, ordered by earliest original source-fact
position; each operation retains its original fact and alias order.
terminal_hash binds the first six fields plus the independently authenticated
sorted recovery and supersession receipt hash lists. Projection hashing binds
this terminal object only when present; old absent-field completed proof bytes
and single-root validation remain unchanged.

Runtime acquires missing leaves, free-accepts guarded received results, adopts
all paid whole-operation refinements exactly once and retains unconsumed
ancestor operations. A terminal frontier may span more than 128 operations or
multiple page outputs. No further metadata catalogue request is needed after
complete leaf coverage. Missing/orphan/double-consumed pages, unknown work or
unfinished authorizations remain blockers. Current, stored and support readers
recompute the same exact frontier; inline terminal assertions have no authority.

`checkpoint_catalogue_preview(snapshot, expected_input_hash, *, read_checkpoint,
inputs)` is the separate explicit direct-input component contract for inspecting
checkpoint structure without persisted SOURCE. It returns no sealable terminal:
terminal_candidate is always null. It cannot validate, save or publish a new
complete-operation-frontier proof. A persisted runtime always uses the strict
source plan; direct-input previews stop before downstream semantics/publication.
Existing absent-terminal completed identities remain unchanged.

### Bounded partition repair

`partition_repair_plan(snapshot, expected_input_hash, *, read_checkpoint,
parent_subdivision_hash, parent_review_hash, allow_running=())` is a read-only reconstruction for
one authenticated accepted subdivision and its exact semantic rejection. It
requires the persisted SOURCE and the complete catalogue frontier. It returns
`planned`, `received`, `accepted` or `blocked`, the complete D2 subdivision
request and identity, and the review identity only after a real D2 subdivision
result exists. Physical tasks remain attempt 1; the closed repair lineage uses
`draft_index: 2`. The optional `allow_running` identifies exact already-owned slots solely for structural reconstruction; it cannot authorize a call. The plan is not permission to send.

A runtime defaults to an empty `partition_repair_allowlist`. New requests need
an explicitly selected parent pair; already received or accepted results can
be replayed without another paid call. A failed second review or an unknown
provider outcome cannot create a third draft. The optional partition `repair`
proof binds the original rejection and both new task hashes; current, stored and
support validation reauthenticate that chain and retain the original paid
records in the task inventory. Existing partitions without this field preserve
their previous identity.

### Atomic parent budget

`atomic_budget_plan` is a private, read-only reconstruction for one explicitly
selected four-row rejected partition history. It verifies the raw rows and the
persisted SOURCE/frontier before deriving one intact-root packet. The trusted
profile is unchanged. The packet's effective candidate cap is derived from the
complete structured proposal and review templates, threefold candidate reserve
and a 1,024-byte margin. The actual review input is checked before its slot is
claimed. Raw parsed graph and canonical candidate must both fit that cap.

New paid atomic calls require the exact selected tuple in the runtime's
`atomic_budget_allowlist`, which defaults empty. The four earlier partition
rows remain immutable, required and occupied history, not accepted
partitions. Accepted or received checkpoints can be replayed without paying
again. The optional private `semantic_coverage.atomic_budgets` proof is checked
against persisted rows and an actual accepted semantic review; old projections
without this field retain their existing identity.

### Whole operation exact fit

`whole_operation_plans(snapshot, input_hash, *, read_checkpoint, selected_history_pairs=(), atomic_budget_chains=(), allow_running=(), partition_repair_pairs=())` is a private read-only batch reconstruction over one frozen SOURCE and physical catalogue forest. All selections default empty; the additional repair and running arguments authenticate existing structure and never authorize dispatch. A fresh oversized root may use `whole-operation-exact-fit-v1` only with the fixed local profile, complete original inputs, an actual first proposal within 64,000 bytes and a candidate estimate within 40,000 bytes. An explicitly selected two-row rejected partition history remains required and occupied. Ordinary fitted packets and the older atomic contract retain their identity.

Both raw and canonical graphs must fit 40,000 bytes and be independently consistent. The actual complete review wire is checked against 160,000 bytes before reserving any review row. If it exceeds that limit, the paid first proposal is retained and the run fails closed without reserving or sending a review. A real quality rejection keeps the existing prior and preservation contract. All revisions share two drafts; the second proposal is checked against its actual 64,000-byte limit before its slot is reserved. No path authorizes a third draft or retries an unknown outcome. Optional `semantic_coverage.whole_operations` proofs are independently reconnected to the source, raw checkpoints, complete root and accepted review by current, stored and support consumers.

### 私有普通提案超时的一次显式恢复（非 HTTP API）

`semantic_proposal_recovery.proposal_timeout_recovery_plan(snapshot, run, parent_stage_hash, *, read_checkpoint, atomic_budget_chains=(), whole_histories=(), partition_repair_pairs=())` 只读重建完整 SOURCE、目录和已接受分区，并为精确的普通 D1 提案超时生成封闭计划。三组历史参数仅指定需要认证的已存在结构，不授予调用权限；省略时为空。原始行、输入、请求、模型目的地、策略及授权时库存均受计划约束。历史库存摘要不是要求后续库存保持不变的断言；来源摘要绑定稳定 run 身份与有序模块 ID，并保留完整 chunks、addenda、matching 和 L0，运行进度、错误与更新时间不改变恢复身份。

`authorize_proposal_timeout_recovery(service, snapshot, run, plan, *, authorization=path)` 在项目锁和暂停控制下，以私有 0600 授权与本地进程已退出证据核对当前输入、库存和计划，事务内创建一条回执及一个 planned child；原失败行不修改。仅 `operator-normal-proposal-timeout-once-v1` 的精确 `proposal / attempt=1 / needs_review / model_call / 408 / timeout / null result` 可选；502、评审、第二草稿、whole/atomic 和其他恢复子项不适用。回执明确记录上游结果与计费未对账，不能推断远端完成、取消或退款。

恢复执行者必须显式为 `proposal_timeout_recovery_allowlist` 提供完整 `(parent_stage_hash, child_stage_hash)`；默认空集合。消费在项目锁及事务内重新检查完整库存和控制并以 CAS 一次性标记，再调用模型；消费后崩溃或失败不能重发。原父及子项均占用物理调用，精确包最多 5 次（原本 4 次加本次 1 次替代），最多两次逻辑草稿。评审引用实际 child，质量修改引用该 child 及其真实失败；其他包及 whole/atomic 上限不变。回执摘要和原父、子项共同纳入最终证明。此控制不提供公开 HTTP 入口，也不自动恢复未选择的超时。
