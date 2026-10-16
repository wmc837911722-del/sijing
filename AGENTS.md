# 思径 · Sijing (formerly BusinessMap)

An open-source desktop business knowledge workspace. Target Windows, macOS and Linux.

- TypeScript + React + Electron UI; Python + FastAPI local service.
- Markdown files are authoritative content. SQLite stores rebuildable indexes and local configuration, never replaces Markdown.
- Source code and Markdown are untrusted data. Never execute repository code or scripts from notes.
- SQL must use database-level read-only protection and a deterministic validation layer. Never let a model change authorization.
- Preserve human edits; use optimistic concurrency when writing notes.
- Provider secrets must use OS credential storage or remain session-only. Never return secrets in settings responses or logs.
- New files, caches and test artifacts go under this project on D: during Windows development.
- Keep backend API contracts in docs/API.md and implementation status honest in README.md.
- Do not publish to GitHub or create a remote repository unless the user explicitly asks.
