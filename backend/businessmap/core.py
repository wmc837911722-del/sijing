"""Markdown is authoritative; SQLite holds configuration and rebuildable state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import yaml


class CoreError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def revision(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def slug(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .")
    if not value or value.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        value = "笔记"
    return value[:100]


DEMO_NOTES = {
    "开始阅读.md": """---
title: 业务知识库 · 示例空间
type: overview
tags: [示例, 开始]
demo: true
---
# 业务知识库 · 示例空间

> **演示内容**：这里的电商流程与数据库都是本地生成的示例，不来自你的真实代码或业务数据。

这个空间由真实的 Markdown 文件组成。你可以编辑笔记、使用 `[[双向链接]]`，也可以用其他编辑器修改文件。

从 [[业务/订单流程]] 开始，沿着 [[业务/支付与成交额]]、[[业务/退款规则]] 探索业务。

[[数据/订单表]] 与 [[数据/退款表]] 说明了本地示例数据库的字段。要使用自己的业务知识，请接入仓库，或打开自己的知识库目录。

仓库接入先生成明确标记的代码文件索引。选择并配置模型后，才可主动发起 AI 业务分析。
""",
    "业务/订单流程.md": """---
title: 订单流程
type: process
tags: [示例, 订单]
demo: true
---
# 订单流程

> 示例业务规则，不代表真实项目。

订单的示例状态为 `pending`（待支付）、`paid`（已支付）、`completed`（已完成）、`cancelled`（已取消）。

创建订单 → 等待支付 → [[业务/支付与成交额]] → 完成订单。已取消订单不计入有效销售。

订单保存在 [[数据/订单表]]。退款流程见 [[业务/退款规则]]。
""",
    "业务/支付与成交额.md": """---
title: 支付与成交额
type: rule
tags: [示例, 支付, 统计口径]
demo: true
---
# 支付与成交额

> 示例统计口径；上线使用前应由实际代码和业务负责人确认。

示例成交额为状态 `paid` 或 `completed` 的订单 `amount` 之和，金额单位为人民币元。

示例净成交额 = 成交额 − 状态 `completed` 的退款金额。退款按退款记录的创建时间统计时，与按原订单时间统计的口径不同，查询时需明确。

数据来自 [[数据/订单表]] 和 [[数据/退款表]]，业务背景见 [[业务/订单流程]] 与 [[业务/退款规则]]。
""",
    "业务/退款规则.md": """---
title: 退款规则
type: rule
tags: [示例, 退款]
demo: true
---
# 退款规则

> 这是人工编写的演示规则，不是 AI 对真实仓库的分析结论。

示例中已支付或已完成的订单可以提交退款申请。退款状态 `pending` 表示处理中，`completed` 表示退款完成。

退款金额应不超过对应订单支付金额；同一订单可能存在多条退款记录，因此汇总时应先按订单聚合退款，避免 JOIN 导致金额重复。

关联：[[业务/订单流程]]、[[业务/支付与成交额]]、[[数据/退款表]]。
""",
    "数据/订单表.md": """---
title: 订单表
type: entity
tags: [示例, 数据模型]
demo: true
---
# 订单表

> 本地示例数据库的 `orders` 表，无真实客户数据。

| 字段 | 含义 |
| --- | --- |
| id | 订单编号 |
| status | pending / paid / completed / cancelled |
| channel | web / app / store |
| amount | 金额（人民币元） |
| created_at | 创建时间，示例固定为 2026 年 9 月 |

业务说明：[[业务/订单流程]]、[[业务/支付与成交额]]。通过 `orders.id = refunds.order_id` 关联 [[数据/退款表]]。
""",
    "数据/退款表.md": """---
title: 退款表
type: entity
tags: [示例, 数据模型]
demo: true
---
# 退款表

> 本地示例数据库的 `refunds` 表。

| 字段 | 含义 |
| --- | --- |
| id | 退款编号 |
| order_id | 对应 orders.id |
| amount | 退款金额（人民币元） |
| status | pending / completed |
| created_at | 退款记录创建时间 |

关联 [[数据/订单表]]，规则见 [[业务/退款规则]]。仅已完成退款计入示例 [[业务/支付与成交额|净成交额]] 扣减。
""",
}


class Store:
    MAX_NOTE_BYTES = 2 * 1024 * 1024
    MAX_REPO_FILES = 500
    SOURCE_SUFFIXES = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".go", ".rs",
        ".cs", ".php", ".rb", ".vue", ".sql", ".prisma", ".graphql", ".proto",
    }

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="businessmap-job")
        self._poller: threading.Thread | None = None
        self._polling_repos: set[str] = set()
        self._closed = False
        self._cache: dict[str, dict] = {}
        self._signature: dict[str, tuple] = {}
        self._vault_epoch = 0
        self.on_repository_synced: Callable[[str, list[str]], None] | None = None
        self.db_path = self.data_dir / "workspace.sqlite"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS repositories (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS activities (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS managed_notes (
                    repository_id TEXT NOT NULL, source_path TEXT NOT NULL, note_path TEXT NOT NULL,
                    blob TEXT NOT NULL, revision TEXT NOT NULL,
                    PRIMARY KEY(repository_id, source_path));
            """)
        existing_vault = self.config_get("vault_path")
        self.vault = Path(existing_vault).resolve() if existing_vault else self.data_dir / "vault"
        self.vault.mkdir(parents=True, exist_ok=True)
        if not existing_vault:
            self.config_set("vault_path", str(self.vault))
            if not any(self.vault.iterdir()):
                for name, content in DEMO_NOTES.items():
                    target = self.vault / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                self.config_set("demo_vault_path", str(self.vault))
                self.activity("示例知识库已创建", "所有业务笔记和数据库均为演示数据", "success")
        self._init_demo_db()
        self.refresh()
        from .git_transport import GitTransport
        self.git_transport = GitTransport(self)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def config_get(self, key: str, default: Any = None) -> Any:
        with self._lock, self._db() as db:
            row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def config_set(self, key: str, value: Any) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def config_update(self, key: str, updater: Callable, default: Any = None) -> Any:
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
            value = updater(json.loads(row[0]) if row else default)
            db.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))
            return value

    @property
    def version(self) -> int:
        return self.config_get("knowledge_version", 0)

    @property
    def vault(self) -> Path:
        # The desktop and a separately launched MCP process share this setting.
        # Check it at the filesystem boundary rather than keeping a stale path
        # for the lifetime of an external AI connection.
        with self._lock:
            configured = self.config_get("vault_path")
            if configured:
                selected = Path(configured).resolve()
                if selected != self._vault:
                    self._vault = selected
                    self._signature = {}
                    self._cache = {}
                    self._vault_epoch += 1
            return self._vault

    @vault.setter
    def vault(self, value: Path) -> None:
        self._vault = value

    def _init_demo_db(self) -> None:
        path = self.data_dir / "demo.sqlite"
        if path.exists():
            return
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT, channel TEXT, amount REAL, created_at TEXT);
                CREATE TABLE refunds(id INTEGER PRIMARY KEY, order_id INTEGER, amount REAL, status TEXT, created_at TEXT);
            """)
            db.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", [
                (1, "completed", "web", 299.0, "2026-09-02T09:00:00+08:00"),
                (2, "paid", "app", 158.0, "2026-09-03T10:00:00+08:00"),
                (3, "cancelled", "web", 99.0, "2026-09-05T11:00:00+08:00"),
                (4, "completed", "store", 520.0, "2026-09-08T12:00:00+08:00"),
                (5, "completed", "app", 680.0, "2026-09-12T13:00:00+08:00"),
                (6, "pending", "web", 88.0, "2026-09-15T14:00:00+08:00"),
                (7, "completed", "web", 399.0, "2026-09-20T15:00:00+08:00"),
                (8, "paid", "store", 240.0, "2026-09-23T16:00:00+08:00"),
            ])
            db.executemany("INSERT INTO refunds VALUES (?,?,?,?,?)", [
                (1, 1, 50.0, "completed", "2026-09-09T09:00:00+08:00"),
                (2, 5, 120.0, "completed", "2026-09-18T09:00:00+08:00"),
                (3, 7, 80.0, "pending", "2026-09-25T09:00:00+08:00"),
                (4, 1, 20.0, "completed", "2026-09-28T09:00:00+08:00"),
            ])

    def _path(self, path: str, *, vault: Path | None = None) -> Path:
        pure = PurePosixPath(path)
        if (not path or len(path) > 1024 or pure.as_posix() != path
                or "\\" in path or re.search(r'[<>:"|?*\x00-\x1f]', path) or pure.is_absolute()
                or any(part in {"..", "."} or part.startswith(".") for part in pure.parts)
                or pure.suffix.lower() != ".md"):
            raise CoreError(400, "笔记路径必须是知识库内的相对 .md 路径")
        vault = self.vault if vault is None else vault
        target = vault.joinpath(*pure.parts)
        if not target.resolve().is_relative_to(vault.resolve()):
            raise CoreError(400, "笔记路径不能指向知识库之外")
        return target

    def _summarize(self, path: str, content: str, modified: float) -> dict:
        body, meta = content, {}
        match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", content, re.S)
        if match:
            try:
                parsed = yaml.safe_load(match.group(1))
                if isinstance(parsed, dict):
                    meta = parsed
                body = content[match.end():]
            except yaml.YAMLError:
                pass
        heading = re.search(r"(?m)^#\s+(.+)$", body)
        tags = meta.get("tags", [])
        if isinstance(tags, str):
            tags = re.split(r"[,\s]+", tags)
        if not isinstance(tags, list):
            tags = []
        tags = list(dict.fromkeys(str(tag).lstrip("#") for tag in tags if isinstance(tag, (str, int))))
        title = meta.get("title") or (heading.group(1).strip() if heading else PurePosixPath(path).stem)
        excerpt = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m[2] or m[1], body)
        excerpt = re.sub(r"[#>*`|]", "", excerpt)
        excerpt = re.sub(r"\s+", " ", excerpt).strip()[:180]
        return {
            "id": path, "path": path, "title": str(title), "type": str(meta.get("type", "note")),
            "tags": tags, "updated_at": datetime.fromtimestamp(modified, timezone.utc).isoformat(),
            "excerpt": excerpt, "content": content, "revision": revision(content),
        }

    def refresh(self) -> None:
        with self._lock:
            vault = self.vault
            signature = {}
            for target in vault.rglob("*.md"):
                relative = target.relative_to(vault)
                if any(part.startswith(".") for part in relative.parts) or target.is_symlink():
                    continue
                if not target.resolve().is_relative_to(vault):
                    continue
                try:
                    stat = target.stat()
                    if stat.st_size > self.MAX_NOTE_BYTES:
                        continue
                    name = relative.as_posix()
                    fingerprint = (stat.st_mtime_ns, stat.st_size)
                    signature[name] = fingerprint
                    if self._signature.get(name) != fingerprint:
                        content = target.read_text(encoding="utf-8-sig")
                        self._cache[name] = self._summarize(name, content, stat.st_mtime)
                except (OSError, UnicodeError):
                    continue
            # A separate process can select another vault while this scan runs.
            # The getter clears the old cache before rejecting stale results.
            if self.vault != vault:
                raise CoreError(409, "知识库已切换，请重新读取")
            for name in self._cache.keys() - signature.keys():
                self._cache.pop(name, None)
            if signature != self._signature:
                self._signature = signature
                self.config_set("knowledge_version", self.version + 1)

    @staticmethod
    def _summary(note: dict) -> dict:
        return {key: value for key, value in note.items() if key not in {"content", "revision"}}

    def notes(self, query: str = "") -> list[dict]:
        self.refresh()
        with self._lock:
            terms = query.casefold().split()
            result = [self._summary(note) for note in self._cache.values()
                      if all(term in (note["title"] + " " + note["content"] + " " + note["path"]).casefold() for term in terms)]
            return sorted(result, key=lambda item: (item["updated_at"], item["path"]), reverse=True)

    def _links(self, source: str, content: str) -> list[str]:
        # Links inside fenced code or inline code are examples, not relationships.
        text = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1\s*$", "", content)
        text = re.sub(r"`[^`\n]*`", "", text)
        result = []
        for raw in re.findall(r"\[\[([^\]\n]+)\]\]", text):
            dest = raw.split("|", 1)[0].split("#", 1)[0].strip()
            if not dest:
                dest = source
            if not dest.endswith(".md"):
                dest += ".md"
            candidates = [dest, (PurePosixPath(source).parent / dest).as_posix()]
            matches = [candidate for candidate in candidates if candidate in self._cache]
            if not matches:
                matches = [name for name, note in self._cache.items()
                           if PurePosixPath(name).name.casefold() == dest.casefold()
                           or note["title"].casefold() == dest[:-3].casefold()]
            # Ambiguous bare names deliberately do not create a misleading edge.
            if len(set(matches)) == 1 and matches[0] not in result:
                result.append(matches[0])
        return result

    def read_note(self, path: str) -> dict:
        self._path(path)
        self.refresh()
        with self._lock:
            if path not in self._cache:
                raise CoreError(404, "笔记不存在或不是可读取的 UTF-8 Markdown 文件")
            note = dict(self._cache[path])
            note["links"] = [self._summary(self._cache[name]) for name in self._links(path, note["content"])]
            note["backlinks"] = [self._summary(other) for name, other in self._cache.items()
                                 if name != path and path in self._links(name, other["content"])]
            return note

    def _write_note(self, path: str, content: str, revision: str | None = None,
                    *, vault: Path | None = None) -> str:
        """Write with the same revision guard, without rebuilding read indexes."""
        if len(content.encode("utf-8")) > self.MAX_NOTE_BYTES:
            raise CoreError(413, "笔记过大（上限 2 MB）")
        with self._lock:
            selected = self.vault
            if vault is not None and selected != vault:
                raise CoreError(409, "知识库已切换，请重新发起仓库同步")
            target = self._path(path, vault=selected)
            if target.exists():
                current = target.read_text(encoding="utf-8-sig")
                if revision is None or globals()["revision"](current) != revision:
                    raise CoreError(409, "笔记已变化，请重新读取并合并修改后保存")
            elif revision is not None:
                raise CoreError(409, "笔记已被删除，请重新确认后创建")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Replace atomically; other readers never see a partially written note.
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(content, encoding="utf-8", newline="")
                if self.vault != selected:
                    raise CoreError(409, "知识库已切换，请重新读取后保存")
                current_revision = (globals()["revision"](target.read_text(encoding="utf-8-sig"))
                                    if target.exists() else None)
                if current_revision != revision:
                    raise CoreError(409, "保存期间笔记发生外部变化，请重新读取")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            self._signature.pop(path, None)
            return globals()["revision"](content)

    def save_note(self, path: str, content: str, revision: str | None = None) -> dict:
        with self._lock:
            self._write_note(path, content, revision)
            self.refresh()
            return self.read_note(path)

    def create_note(self, title: str, folder: str = "") -> dict:
        if not title.strip():
            raise CoreError(400, "请输入笔记标题")
        with self._lock:
            base = (PurePosixPath(folder) / slug(title)).as_posix()
            candidate, number = base + ".md", 2
            while self._path(candidate).exists():
                candidate = f"{base} {number}.md"
                number += 1
            note = self.save_note(candidate, f"# {title.strip()}\n\n")
            self.activity("新建笔记", candidate, "note")
            return note

    def graph(self) -> dict:
        self.refresh()
        from .content_layout import is_source_document
        with self._lock:
            visible = {path: note for path, note in self._cache.items() if not is_source_document(note)}
            return {
                "nodes": [{key: note[key] for key in ("id", "title", "type", "tags")} for note in visible.values()],
                "edges": [{"source": name, "target": dest} for name, note in visible.items()
                          for dest in self._links(name, note["content"]) if dest in visible],
            }

    def migrate_content_layout(self) -> dict:
        from .layout_migration import migrate_content_layout
        return migrate_content_layout(self)

    def open_vault(self, path: str) -> dict:
        if not Path(path).expanduser().is_absolute():
            raise CoreError(400, "请选择知识库的绝对目录路径")
        target = Path(path).expanduser().resolve()
        forbidden = {Path(target.anchor), Path.home().resolve()}
        for value in (os.environ.get("WINDIR"), os.environ.get("PROGRAMFILES")):
            if value:
                forbidden.add(Path(value).resolve())
        if target in forbidden or any(part in {".git", ".ssh", ".codex"} for part in target.parts):
            raise CoreError(400, "请选择专用知识库目录，而不是系统、用户根目录或配置目录")
        with self._lock:
            if any(task["status"] in {"queued", "running"} for task in self.tasks()):
                raise CoreError(409, "后台任务正在运行，请完成后再切换知识库")
            target.mkdir(parents=True, exist_ok=True)
            self.vault = target
            self._vault_epoch += 1
            self.config_set("vault_path", str(target))
            self._signature = {}
            self._cache = {}
            self.refresh()
            self.activity("已打开知识库", str(target), "success")
        return self.workspace()

    def activity(self, title: str, detail: str = "", kind: str = "info") -> None:
        item = {"id": uuid.uuid4().hex, "title": title, "detail": detail, "kind": kind, "created_at": now()}
        with self._lock, self._db() as db:
            db.execute("INSERT INTO activities VALUES (?,?)", (item["id"], json.dumps(item, ensure_ascii=False)))
            db.execute("DELETE FROM activities WHERE rowid NOT IN (SELECT rowid FROM activities ORDER BY rowid DESC LIMIT 200)")

    def activities(self) -> list[dict]:
        with self._lock, self._db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM activities ORDER BY rowid DESC LIMIT 50")]

    def repositories(self) -> list[dict]:
        with self._lock, self._db() as db:
            repositories = [json.loads(row[0]) for row in db.execute("SELECT data FROM repositories ORDER BY rowid DESC")
                            if json.loads(row[0]).get("vault_path") == str(self.vault)]
            for repository in repositories:
                row = db.execute("SELECT value FROM config WHERE key=?", (f"ai_analysis:{repository['id']}",)).fetchone()
                repository["ai_auto_sync"] = bool(json.loads(row[0]).get("auto_enabled", False)) if row else False
                repository["connection"] = self.git_transport.public(repository)
            return repositories

    def get_repository(self, repo_id: str) -> dict:
        matches = [item for item in self.repositories() if item["id"] == repo_id]
        if not matches:
            raise CoreError(404, "仓库不存在于当前知识库")
        return matches[0]

    def _write_repo(self, repo: dict) -> None:
        repo = {key: value for key, value in repo.items() if key != "ai_auto_sync"}
        if "connection" in repo:
            repo["connection"] = {key: value for key, value in repo["connection"].items()
                                  if key in self.git_transport.FIELDS}
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO repositories VALUES (?,?)", (repo["id"], json.dumps(repo, ensure_ascii=False)))

    def update_repository(self, repo_id: str, changes: dict) -> dict:
        with self._lock:
            repo = self.get_repository(repo_id)
            repo.update(changes)
            self._write_repo(repo)
            return self.get_repository(repo_id)

    def update_repository_connection(self, repo_id: str, connection: dict, branch: str | None = None) -> dict:
        with self._lock:
            repo = self.get_repository(repo_id)
            if repo_id in self._polling_repos or any(task["status"] in {"queued", "running"} and task.get("payload", {}).get("repository_id") == repo_id
                   for task in self.tasks()):
                raise CoreError(409, "仓库正在处理，请完成后再修改连接设置。")
            normalized = self.git_transport.normalize(repo["url"], connection)
            self.git_transport.resolve_secret(normalized, connection, repo_id)
            changes = {"connection": normalized, "last_error": None}
            if branch is not None and repo["remote"]:
                changes["branch"] = self.git_transport.branch(branch)
            self.git_transport.save_secret(repo_id, normalized, connection)
            if repo.get("commit"):
                changes["status"] = "ready"
            updated = self.update_repository(repo_id, changes)
            self.git_transport.cleanup_secrets(repo_id)
            return updated

    def workspace(self) -> dict:
        graph = self.graph()
        repos = self.repositories()
        from .content_layout import is_source_document
        with self._lock:
            note_count = len(self._cache)
            source_count = sum(is_source_document(note) for note in self._cache.values())
        return {"name": "业务知识库", "vault_path": str(self.vault), "version": self.version,
                "demo": self.config_get("demo_vault_path") == str(self.vault),
                "stats": {"notes": note_count, "business_notes": note_count - source_count,
                          "source_documents": source_count, "links": len(graph["edges"]), "repositories": len(repos)},
                "repositories": repos, "recent_activity": self.activities()[:8]}

    def tasks(self) -> list[dict]:
        with self._lock, self._db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM tasks ORDER BY rowid DESC LIMIT 100")]

    def _write_task(self, task: dict) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?)", (task["id"], json.dumps(task, ensure_ascii=False)))

    def _read_task(self, task_id: str) -> dict | None:
        with self._lock, self._db() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def create_task(self, kind: str, payload: dict | None = None) -> dict:
        task = {"id": uuid.uuid4().hex, "kind": kind, "payload": payload or {}, "status": "queued",
                "progress": 0, "message": "等待处理", "created_at": now()}
        self._write_task(task)
        return task

    def update_task(self, task_id: str, progress: int, message: str) -> None:
        with self._lock:
            task = self._read_task(task_id)
            if task:
                task.update(progress=max(0, min(100, progress)), message=message)
                self._write_task(task)

    def run_task(self, task_id: str, function: Callable[[str], Any]) -> None:
        def run() -> None:
            with self._lock:
                task = self._read_task(task_id)
                if task is None:
                    return
                task.update(status="running", message="正在处理")
                self._write_task(task)
            try:
                function(task_id)
                task = self._read_task(task_id) or task
                message = task.get("message", "")
                task.update(status="completed", progress=100,
                            message=message if message not in {"", "等待处理", "正在处理"} else "处理完成")
            except Exception as exc:
                task = self._read_task(task_id) or task
                task.update(status="failed", message="处理失败", error=str(exc)[:500])
                self.activity("后台任务失败", str(exc)[:300], "error")
            self._write_task(task)
        self._executor.submit(run)

    def start(self) -> None:
        if self._poller:
            return
        for task in self.tasks():
            if task["status"] in {"queued", "running"}:
                task.update(status="failed", message="上次运行被中断", error="服务重启，请重新发起任务")
                self._write_task(task)
        for repo in self.repositories():
            if repo["status"] in {"pending", "syncing"}:
                repo["status"] = "error"
                self._write_repo(repo)
        def poll() -> None:
            tick = 0
            while not self._stop.wait(3):
                try:
                    self.refresh()
                    tick += 1
                    if tick % 20 == 0:
                        for repo in self.repositories():
                            if repo.get("auto_sync") and repo["status"] == "ready":
                                with self._lock:
                                    if repo["id"] in self._polling_repos or self._closed:
                                        continue
                                    self._polling_repos.add(repo["id"])
                                self._executor.submit(self._poll_repository, repo["id"])
                except Exception:
                    # A temporarily unavailable vault must not kill later refreshes.
                    continue
        self._poller = threading.Thread(target=poll, daemon=True, name="businessmap-vault")
        self._poller.start()

    def _poll_repository(self, repo_id: str) -> None:
        """Silent freshness checks do not create user-visible no-op tasks."""
        try:
            repo = self.get_repository(repo_id)
            if self._closed or repo["status"] != "ready":
                return
            path = Path(repo["path"])
            if repo["remote"]:
                if not repo.get("branch"):
                    self.sync_repository(repo_id)
                    return
                head = self.git_transport.for_repository(repo, ["fetch", "--depth=1", "--no-tags", "--", repo["url"], "refs/heads/" + repo["branch"]], path).strip()
            else:
                head = self._git(["rev-parse", "HEAD"], path).strip()
            self.config_set(f"repository_poll_error:{repo_id}", None)
            if repo.get("last_error"):
                self.update_repository(repo_id, {"last_error": None})
            if head != repo["commit"] and not self._closed:
                self.sync_repository(repo_id)
        except Exception as exc:
            detail = str(exc)[:300]
            try:
                self.update_repository(repo_id, {"last_error": {"code": getattr(exc, "code", "git"), "message": detail}})
            except CoreError:
                pass
            key = f"repository_poll_error:{repo_id}"
            if self.config_get(key) != detail:
                self.config_set(key, detail)
                self.activity("仓库自动检查暂时失败", detail, "error")
        finally:
            with self._lock:
                self._polling_repos.discard(repo_id)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._poller:
            self._poller.join(timeout=4)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _git(self, args: list[str], cwd: Path | None = None, timeout: int = 30) -> str:
        return self.git_transport.run(args, cwd, timeout)

    def add_repository(self, url: str, name: str | None = None, branch: str = "", connection: dict | None = None) -> dict:
        from urllib.parse import urlsplit
        url, remote = self.git_transport.endpoint(url)
        normalized = self.git_transport.normalize(url, connection)
        self.git_transport.resolve_secret(normalized, connection)
        branch = self.git_transport.branch(branch) if remote else ""
        if remote:
            parts = urlsplit(url)
            source_path = parts.path if "://" in url else url.split(":", 1)[1]
            display_name = name or PurePosixPath(source_path.rstrip("/")).name.removesuffix(".git") or "repository"
            repo_path = self.data_dir / "repositories" / uuid.uuid4().hex
        else:
            repo_path = Path(url).expanduser().resolve()
            if not repo_path.is_dir():
                raise CoreError(400, "本地仓库目录不存在")
            # Read-only Git metadata inspection, never execute scripts from the repo.
            self._git(["rev-parse", "--git-dir"], repo_path)
            display_name = name or repo_path.name
        repo = {"id": uuid.uuid4().hex, "name": display_name[:100], "url": url, "path": str(repo_path),
                "branch": branch, "commit": "", "status": "pending", "last_sync": None,
                "auto_sync": True, "remote": remote, "vault_path": str(self.vault),
                "connection": normalized, "last_error": None}
        self.git_transport.save_secret(repo["id"], normalized, connection)
        self._write_repo(repo)
        self.sync_repository(repo["id"])
        return self.get_repository(repo["id"])

    def sync_repository(self, repo_id: str) -> dict:
        with self._lock:
            repo = self.get_repository(repo_id)
            for task in self.tasks():
                if task["status"] in {"queued", "running"} and task.get("payload", {}).get("repository_id") == repo_id:
                    return {"task_id": task["id"]}
            task = self.create_task("repository-sync", {"repository_id": repo_id})
            repo["status"] = "syncing"
            self._write_repo(repo)
            self.run_task(task["id"], lambda task_id: self._sync_repo(repo_id, task_id))
            return {"task_id": task["id"]}

    @classmethod
    def _safe_source(cls, path: str, size: int) -> bool:
        parts = PurePosixPath(path).parts
        if size > 128_000 or any(part.startswith(".") or part in {"node_modules", "vendor", "dist", "build", "target"} for part in parts):
            return False
        stem = PurePosixPath(path).stem.casefold()
        if any(word in stem for word in ("secret", "credential", "password", "private-key", "private_key")):
            return False
        return PurePosixPath(path).suffix.casefold() in cls.SOURCE_SUFFIXES

    def _source_files(self, repo: dict, commit: str) -> list[dict]:
        output = self._git(["ls-tree", "-r", "-l", "-z", commit], Path(repo["path"]))
        files = []
        for entry in output.split("\0"):
            if not entry or "\t" not in entry:
                continue
            metadata, path = entry.split("\t", 1)
            fields = metadata.split()
            if len(fields) != 4 or fields[1] != "blob" or not fields[3].isdigit():
                continue
            if self._safe_source(path, int(fields[3])):
                files.append({"path": path, "blob": fields[2], "size": int(fields[3])})
        return sorted(files, key=lambda item: item["path"])

    def _sync_repo(self, repo_id: str, task_id: str) -> None:
        repo = self.get_repository(repo_id)
        try:
            path = Path(repo["path"])
            self.update_task(task_id, 10, "正在读取 Git 版本信息")
            cloned = False
            if repo["remote"] and not (path / ".git").exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                args = ["clone", "--depth=1", "--single-branch", "--no-checkout"]
                if repo.get("branch"):
                    args += ["--branch", repo["branch"]]
                self.git_transport.for_repository(repo, [*args, "--", repo["url"], str(path)])
                cloned = True
            if repo["remote"] and not cloned and not repo.get("branch"):
                repo["branch"] = self.git_transport.test(repo["url"], repository_id=repo_id)["default_branch"]
                if not repo["branch"]:
                    raise CoreError(400, "远程仓库未设置默认分支，请在连接设置中选择分支。")
            branch = (repo.get("branch") if repo["remote"] else None) or self._git(["rev-parse", "--abbrev-ref", "HEAD"], path).strip()
            if repo["remote"] and not cloned:
                commit = self.git_transport.for_repository(repo, ["fetch", "--depth=1", "--no-tags", "--", repo["url"], "refs/heads/" + branch], path).strip()
            else:
                commit = self._git(["rev-parse", "HEAD"], path).strip()
            if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
                raise CoreError(400, "Git 返回了无效的提交标识")
            changed_paths = None
            if commit != repo.get("commit"):
                all_files = self._source_files(repo, commit)
                files = all_files[:self.MAX_REPO_FILES]
                with self._db() as db:
                    previous = dict(db.execute("SELECT source_path,blob FROM managed_notes WHERE repository_id=?", (repo_id,)))
                current = {item["path"]: item["blob"] for item in files}
                changed_paths = sorted(name for name in previous.keys() | current.keys() if previous.get(name) != current.get(name))
                self.update_task(task_id, 35, "正在更新代码文件索引（未调用 AI）")
                self._publish_code_index(repo, commit, files, len(all_files), task_id)
                self.activity("代码索引已更新", f"{repo['name']} · {commit[:8]} · {len(files)} 个源文件；未进行 AI 业务分析", "success")
            repo.update(branch=branch, commit=commit, status="ready", last_sync=now(), last_error=None)
            self._write_repo(repo)
            self.update_task(task_id, 95, "索引完成")
            if changed_paths is not None and self.on_repository_synced:
                try:
                    self.on_repository_synced(repo_id, changed_paths)
                except Exception as exc:
                    self.activity("自动 AI 更新未能排队", str(exc)[:300], "error")
        except Exception as exc:
            repo["status"] = "error"
            repo["last_error"] = {"code": getattr(exc, "code", "git"), "message": str(exc)[:300]}
            self._write_repo(repo)
            raise

    def _publish_code_index(self, repo: dict, commit: str, files: list[dict], total: int,
                            task_id: str | None = None) -> None:
        from .content_layout import source_note_path
        vault = Path(repo["vault_path"]).resolve()
        with self._lock:
            if self.vault != vault:
                raise CoreError(409, "知识库已切换，请重新发起仓库同步")
            with self._db() as db:
                existing = {row[0]: {"note_path": row[1], "blob": row[2], "revision": row[3]}
                            for row in db.execute("SELECT source_path,note_path,blob,revision FROM managed_notes WHERE repository_id=?", (repo["id"],))}
        new_paths = {item["path"] for item in files}
        preserved = []
        written = 0
        for old, info in existing.items():
            if old in new_paths:
                continue
            with self._lock:
                if self.vault != vault:
                    raise CoreError(409, "知识库已切换，请重新发起仓库同步")
                target = self._path(info["note_path"], vault=vault)
                if target.exists():
                    content = target.read_text(encoding="utf-8-sig")
                    if revision(content) == info["revision"]:
                        target.unlink()
                    else:
                        preserved.append(old)
                        marker = "\n\n> 系统提示：源文件已删除、重命名或不再属于当前索引范围；本笔记因包含人工修改而保留。\n"
                        if marker not in content:
                            self._write_note(info["note_path"], content + marker, revision(content), vault=vault)
                            written += 1
                with self._db() as db:
                    db.execute("DELETE FROM managed_notes WHERE repository_id=? AND source_path=?", (repo["id"], old))
        links = []
        for index, item in enumerate(files, 1):
            source = item["path"]
            old = existing.get(source)
            note_path = source_note_path(repo["id"], hashlib.sha256(source.encode()).hexdigest()[:16] + ".md")
            links.append(f"- [[{note_path[:-3]}|{source}]]")
            meta = yaml.safe_dump({"title": source, "type": "code", "tags": ["代码索引", "非AI分析"],
                                   "source_path": source, "source_commit": commit, "repository_id": repo["id"]},
                                  allow_unicode=True, sort_keys=False).strip()
            content = f"---\n{meta}\n---\n# {source}\n\n> 自动生成的代码文件索引，**不是 AI 业务分析结论**。\n\n- 仓库：{repo['name']}\n- 源文件：`{source}`\n- 提交：`{commit}`\n- 文件大小：{item['size']} 字节\n\n请在仓库面板主动发起 AI 分析，或人工补充业务含义。\n"
            with self._lock:
                if self.vault != vault:
                    raise CoreError(409, "知识库已切换，请重新发起仓库同步")
                target = self._path(note_path, vault=vault)
                if not (old and old["blob"] == item["blob"] and target.exists()):
                    previous = target.read_text(encoding="utf-8-sig") if target.exists() else None
                    if previous is not None and (not old or revision(previous) != old["revision"]):
                        preserved.append(source)
                        proposal = source_note_path(repo["id"], f"更新建议/{hashlib.sha256(source.encode()).hexdigest()[:16]}-{commit[:8]}.md")
                        if not self._path(proposal, vault=vault).exists():
                            self._write_note(proposal, content + f"\n原笔记包含人工修改，未覆盖：[[{note_path[:-3]}]]。\n", vault=vault)
                            written += 1
                    else:
                        saved_revision = self._write_note(note_path, content, revision(previous) if previous is not None else None, vault=vault)
                        written += 1
                        with self._db() as db:
                            db.execute("INSERT OR REPLACE INTO managed_notes VALUES (?,?,?,?,?)",
                                       (repo["id"], source, note_path, item["blob"], saved_revision))
            if task_id and (index % 10 == 0 or index == len(files)):
                self.update_task(task_id, 35 + 55 * index // len(files),
                                 f"已处理 {index} / {len(files)} 个源文件，写入 {written} 篇笔记（未调用 AI）")
        # Each commit has an immutable summary so manual edits are never overwritten.
        summary_path = source_note_path(repo["id"], f"版本-{commit[:12]}.md")
        with self._lock:
            if self.vault != vault:
                raise CoreError(409, "知识库已切换，请重新发起仓库同步")
            if not self._path(summary_path, vault=vault).exists():
                text = f"# {repo['name']} · 代码索引 {commit[:8]}\n\n> 非 AI 分析：这里只记录 Git 提交中的文件清单，不推断业务规则。\n\n版本：`{commit}`\n\n已索引 {len(files)} / {total} 个符合条件的源文件。排除隐藏、敏感命名、依赖目录和大文件。\n\n" + "\n".join(links)
                if preserved:
                    text += "\n\n## 保留人工编辑\n\n以下笔记包含人工修改，未覆盖；变化内容另存更新建议或保留删除提示：\n" + "\n".join(f"- `{name}`" for name in preserved)
                self._write_note(summary_path, text + "\n", vault=vault)
            self.refresh()

    def repository_context(self, repo_id: str) -> dict:
        repo = self.get_repository(repo_id)
        if not repo.get("commit"):
            raise CoreError(409, "仓库尚未完成接入")
        files = self._source_files(repo, repo["commit"])
        # Sample across modules instead of letting an alphabetically early
        # module consume the entire analysis budget. Full source navigation
        # remains available independently of this bounded overview sample.
        groups: dict[str, list[dict]] = {}
        for item in files:
            module = item["path"].split("/", 1)[0] if "/" in item["path"] else "."
            groups.setdefault(module, []).append(item)
        def priority(item):
            path = item["path"].casefold()
            name = PurePosixPath(path).stem
            test = any(part in {"test", "tests", "__tests__"} for part in PurePosixPath(path).parts)
            business = any(word in name for word in ("service", "rule", "state", "policy", "permission", "entity"))
            return (test, not business, item["size"], path)
        for entries in groups.values():
            entries.sort(key=priority)
        ordered = []
        for index in range(30):
            for module in sorted(groups):
                if index < len(groups[module]):
                    ordered.append(groups[module][index])
                if len(ordered) == 30:
                    break
            if len(ordered) == 30:
                break
        per_file_limit = min(10_000, 60_000 // max(1, len(ordered)))
        result, used, shortened = [], 0, False
        for item in ordered:
            if used >= 60_000 or len(result) >= 30:
                break
            # Reading tracked blobs avoids untracked secrets and does not run code.
            content = self._git(["show", f"{repo['commit']}:{item['path']}"], Path(repo["path"]))
            if "\x00" in content:
                continue
            limit = min(per_file_limit, 60_000 - used)
            file_truncated = len(content) > limit
            shortened = shortened or file_truncated
            content = content[:limit]
            content = re.sub(r"(?i)((?:api[_-]?key|password|secret|token)\s*[:=]\s*)['\"][^'\"\r\n]+['\"]", r"\1\"[REDACTED]\"", content)
            result.append({"path": item["path"], "content": content, "truncated": file_truncated})
            used += len(content)
        return {"repository": repo, "commit": repo["commit"], "files": result,
                "total_files": len(files), "total_modules": len(groups),
                "sampled_modules": sorted({item["path"].split("/", 1)[0] if "/" in item["path"] else "." for item in result}),
                "truncated": shortened or len(result) < len(files) or used >= 60_000}
