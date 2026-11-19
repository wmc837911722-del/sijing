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
type: business
content_role: business_knowledge
tags: [示例]
---

# 业务知识库 · 示例空间

> 演示内容：这里的电商流程和示例数据不来自你的真实业务。

从 [[业务/订单流程]] 开始，沿着 [[业务/支付与成交额]]、[[业务/退款规则]] 探索业务。

知识库中的业务文档可以编辑，并通过双向链接互相连接。接入项目并完成分析后，可阅读自己的业务地图。
""",
    "业务/订单流程.md": """---
title: 订单流程
type: business
content_role: business_knowledge
tags: [示例]
---

# 订单流程

> 示例业务，不代表真实项目。

创建订单 → 等待支付 → 已支付 → 完成订单。未完成的订单可以取消，已取消订单不计入有效销售。

支付及收入说明见 [[业务/支付与成交额]]。退款流程见 [[业务/退款规则]]，订单信息见 [[数据/订单表]]。
""",
    "业务/支付与成交额.md": """---
title: 支付与成交额
type: business
content_role: business_knowledge
tags: [示例]
---

# 支付与成交额

> 示例统计口径，实际使用前应由业务负责人确认。

成交额为已支付或已完成订单的金额之和，单位为人民币元。

净成交额为成交额减去已完成退款的金额。按退款发生时间统计，与按原订单创建时间统计可能得出不同结果，必须明确时间范围。

数据含义见 [[数据/订单表]] 和 [[数据/退款表]]，业务背景见 [[业务/订单流程]] 与 [[业务/退款规则]]。
""",
    "业务/退款规则.md": """---
title: 退款规则
type: business
content_role: business_knowledge
tags: [示例]
---

# 退款规则

> 人工编写的演示规则，不代表真实项目分析结论。

已支付或已完成的订单可以提交退款申请。退款处理中不扣减净成交额，退款完成后才扣减。

退款金额不能超过对应订单的支付金额。同一订单可能存在多笔退款，关联订单前应先汇总该订单的退款，避免收入重复计算。

关联 [[业务/订单流程]]、[[业务/支付与成交额]]、[[数据/退款表]]。
""",
    "数据/订单表.md": """---
title: 订单表
type: business
content_role: business_knowledge
tags: [示例]
---

# 订单表

> 本地演示数据，没有真实客户信息。

每笔订单记录订单编号、业务状态、下单渠道、金额和创建时间。金额单位为人民币元；示例渠道包含线上、应用和门店，示例创建时间固定在九月。

订单可处于待支付、已支付、已完成或已取消状态。状态与收入关系见 [[业务/支付与成交额]]。

业务流程见 [[业务/订单流程]]，订单对应的退款见 [[数据/退款表]]。
""",
    "数据/退款表.md": """---
title: 退款表
type: business
content_role: business_knowledge
tags: [示例]
---

# 退款表

> 本地演示数据，没有真实客户信息。

每笔退款记录退款编号、对应订单、退款金额、退款状态和创建时间。同一订单可能有多笔退款。

只有已完成的退款计入净成交额扣减，处理中的退款不扣减。

关联 [[数据/订单表]]，规则见 [[业务/退款规则]]。业务统计口径见 [[业务/支付与成交额]]。
""",
}

# Original shipped demo bytes on Unix and Windows, respectively.
LEGACY_DEMO_DIGESTS = {
    "开始阅读.md": ("fd397f79fa15c72527b28cbbce16bb588ec79128285c5b64b1580012bb381df6", "f7388da84557d067dbdc19260801c8fc336e44010f22145afca0f816d2f653e2"),
    "业务/订单流程.md": ("ce0351527fa102031cf6d3bcb6ee1ce5e6775094dc11ed696a5098026512e5be", "163595d9c28cb70d974f2af5236ccfe5e1649c150bcc4a4cb54f038e968c030e"),
    "业务/支付与成交额.md": ("ee3cecfca27ae11442baa72e0de55b7cc2d02bc37d44fe0c1f9970f862968b80", "c0cd6f03e294c2ad4768500cb80a566f7074c696572fd988526ef94c12efbb1e"),
    "业务/退款规则.md": ("2ad88febf293f45e78009d0a6ddd11ea48ef26b3baa45458a7da1e9081e30eee", "2f2f5c740591a1c12d2b7d1c35c95ed5d45784c544b25dbe39b627a67e8544bb"),
    "数据/订单表.md": ("da3f8e3f310080320b698581895f80f8089bcc528a136d5e6a7bc88999bea6c6", "8347dce36c437ade625e69ea4017a98c8dae46b5c523f271ee1bcd2cc67dc907"),
    "数据/退款表.md": ("48c5ba7228aa8a7dc54fb57c3d32158f3862af4cd722d221390a65d665587eba", "29efd3d8aab79283c9335ee8131473c75ad1d1d3f734efb73421ca61f790a523"),
}

class Store:
    MAX_NOTE_BYTES = 2 * 1024 * 1024
    MAX_REPO_FILES = 500
    SOURCE_SUFFIXES = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".go", ".rs",
        ".cs", ".php", ".rb", ".vue", ".sql", ".prisma", ".graphql", ".proto",
    }

    def __init__(self, data_dir: str | Path, *, refresh_index_on_init: bool = True):
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
        self._link_lookup: dict[str, set[str]] = {}
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
        if refresh_index_on_init:
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
                    self._link_lookup = {}
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

    def _is_default_demo_vault(self, vault: Path) -> bool:
        return vault == self.data_dir / "vault" and self.config_get("demo_vault_path") == str(vault)

    def _index_note(self, target: Path, name: str, modified: float, demo_seed: bool) -> dict:
        if demo_seed:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest in LEGACY_DEMO_DIGESTS[name]:
                note = self._summarize(name, DEMO_NOTES[name], modified)
                # The editor displays the current example but owns only the
                # version of the original file that was actually read.
                note["revision"] = digest
                return note
        return self._summarize(name, target.read_text(encoding="utf-8-sig"), modified)

    def refresh(self) -> None:
        with self._lock:
            vault = self.vault
            demo_vault = self._is_default_demo_vault(vault)
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
                    demo_seed = demo_vault and name in LEGACY_DEMO_DIGESTS
                    fingerprint = (stat.st_mtime_ns, stat.st_size, demo_seed)
                    signature[name] = fingerprint
                    if self._signature.get(name) != fingerprint:
                        self._cache[name] = self._index_note(target, name, stat.st_mtime, demo_seed)
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
                lookup: dict[str, set[str]] = {}
                for name, note in self._cache.items():
                    lookup.setdefault(PurePosixPath(name).name.casefold(), set()).add(name)
                    lookup.setdefault(note["title"].casefold() + ".md", set()).add(name)
                self._link_lookup = lookup
                self.config_set("knowledge_version", self.version + 1)

    @staticmethod
    def _summary(note: dict) -> dict:
        return {key: value for key, value in note.items() if key not in {"content", "revision"}}

    @staticmethod
    def _public_business_metadata(note: dict) -> dict | None:
        from .content_layout import is_source_document
        from .business_content import public_business_metadata, validate_public_business_text
        from .providers import ToolError
        if is_source_document(note):
            return None
        try:
            validate_public_business_text(note["path"])
            validate_public_business_text(note["title"])
            return public_business_metadata(note.get("content", ""))
        except ToolError:
            return None

    @staticmethod
    def public_business_note(note: dict) -> bool:
        return Store._public_business_metadata(note) is not None

    def business_owner_index(self) -> dict:
        """One request-scoped ownership/version snapshot, never a global cache."""
        vault = str(self.vault)
        with self._lock, self._db() as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT key,value FROM config WHERE key LIKE 'project_analysis:%' OR key LIKE 'ai_analysis:%'").fetchall()
            project_row = db.execute("SELECT value FROM config WHERE key='business_projects'").fetchone()
            projects = json.loads(project_row[0]) if project_row else []
            repositories = [json.loads(row[0]) for row in db.execute("SELECT data FROM repositories")]
        context = {"vault_path": vault,
                   "projects": {project["id"]: project for project in projects if project.get("vault_path") == vault},
                   "repositories": {repo["id"]: repo for repo in repositories if repo.get("vault_path") == vault}}
        index = {"path": {}, "revision": {}}
        for config_key, value in rows:
            state = json.loads(value)
            if state.get("vault_path") not in (None, vault):
                continue
            kind, owner_id = config_key.split(":", 1)
            for key, entry in state.get("notes", {}).items():
                owner = {"kind": kind, "id": owner_id, "key": key, "state": state, "entry": entry, "context": context}
                if entry.get("path"):
                    index["path"].setdefault(entry["path"], []).append(owner)
                if entry.get("revision"):
                    index["revision"].setdefault(entry["revision"], []).append(owner)
        if str(self.vault) != vault:
            raise CoreError(409, "知识库已切换，请重新读取业务资料")
        return index

    def business_note_owners(self, note: dict, index: dict | None = None) -> list[dict]:
        """Private ownership survives removing public YAML or moving unchanged files."""
        if "_business_owners" in note:
            return note["_business_owners"]
        index = self.business_owner_index() if index is None else index
        owners = list(index["path"].get(note["path"], []))
        known = {(owner["kind"], owner["id"], owner["key"]) for owner in owners}
        for owner in index["revision"].get(note.get("revision"), []):
            if (owner["kind"], owner["id"], owner["key"]) not in known:
                owners.append(owner)
        return owners

    def current_business_note(self, note: dict, owners: list[dict] | None = None) -> bool:
        meta = self._public_business_metadata(note)
        if meta is None:
            return False
        owners = self.business_note_owners(note) if owners is None else owners
        if not owners:
            # A generated marker with no private owner can never become manual evidence.
            return meta.get("generated") is not True
        for owner in owners:
            state, entry = owner["state"], owner["entry"]
            if (entry.get("revision") != note.get("revision") or entry.get("stale") or entry.get("conflict")):
                return False
            try:
                if owner["kind"] == "project_analysis":
                    project = owner["context"]["projects"][owner["id"]]
                    if state.get("project_revision") != project["revision"]:
                        return False
                    expected = {rid: owner["context"]["repositories"][rid].get("commit") for rid in project["repository_ids"]}
                    sources = entry.get("source_repositories", state.get("source_repositories", []))
                    actual = {source["repository_id"]: source["commit"] for source in sources}
                    if len(actual) != len(sources) or actual != expected:
                        return False
                else:
                    repository = owner["context"]["repositories"][owner["id"]]
                    sources = entry.get("source_repositories")
                    actual = ({source["repository_id"]: source["commit"] for source in sources} if sources else
                              {owner["id"]: entry.get("commit", state.get("last_analyzed_commit"))})
                    if (sources and len(actual) != len(sources)) or actual != {owner["id"]: repository.get("commit")}:
                        return False
            except (CoreError, KeyError, TypeError):
                return False
        return True

    def notes(self, query: str = "") -> list[dict]:
        self.refresh()
        with self._lock:
            terms = query.casefold().split()
            result = [self._summary(note) for note in self._cache.values()
                      if self.public_business_note(note) and all(term in (note["title"] + " " + note["content"] + " " + note["path"]).casefold() for term in terms)]
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
                matches = list(self._link_lookup.get(dest.casefold(), ()))
            # Ambiguous bare names deliberately do not create a misleading edge.
            if len(set(matches)) == 1 and matches[0] not in result:
                result.append(matches[0])
        return result

    def read_note(self, path: str) -> dict:
        self._path(path)
        self.refresh()
        with self._lock:
            if path not in self._cache or not self.public_business_note(self._cache[path]):
                raise CoreError(404, "业务文档不存在或不在公开读取范围内")
            note = dict(self._cache[path])
            note["links"] = [self._summary(self._cache[name]) for name in self._links(path, note["content"]) if self.public_business_note(self._cache[name])]
            note["backlinks"] = [self._summary(other) for name, other in self._cache.items()
                                 if name != path and self.public_business_note(other) and path in self._links(name, other["content"])]
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
            demo_revision = revision in LEGACY_DEMO_DIGESTS.get(path, ()) and self._is_default_demo_vault(selected)
            def disk_revision():
                if demo_revision:
                    return hashlib.sha256(target.read_bytes()).hexdigest()
                return globals()["revision"](target.read_text(encoding="utf-8-sig"))
            if target.exists():
                if revision is None or disk_revision() != revision:
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
                current_revision = disk_revision() if target.exists() else None
                if current_revision != revision:
                    raise CoreError(409, "保存期间笔记发生外部变化，请重新读取")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            self._signature.pop(path, None)
            return globals()["revision"](content)

    def save_note(self, path: str, content: str, revision: str | None = None) -> dict:
        from .business_content import validate_public_business_text
        from .providers import ToolError
        from .content_layout import is_source_document
        self._path(path)
        if is_source_document({"path": path}):
            raise CoreError(422, "此目录仅供内部资料管理，不能保存公开业务文档。")
        try:
            validate_public_business_text(path)
            validate_public_business_text(content)
        except ToolError as error:
            raise CoreError(error.status_code, error.message) from None
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
            visible = {path: note for path, note in self._cache.items() if self.public_business_note(note)}
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
            self._link_lookup = {}
            self.refresh()
            self.activity("已打开知识库", str(target), "success")
        return self.workspace()

    def activity(self, title: str, detail: str = "", kind: str = "info") -> None:
        item = {"id": uuid.uuid4().hex, "title": title, "detail": detail, "kind": kind, "created_at": now()}
        with self._lock, self._db() as db:
            db.execute("INSERT INTO activities VALUES (?,?)", (item["id"], json.dumps(item, ensure_ascii=False)))
            db.execute("DELETE FROM activities WHERE rowid NOT IN (SELECT rowid FROM activities ORDER BY rowid DESC LIMIT 200)")

    def activities(self) -> list[dict]:
        from .business_content import validate_public_business_text
        from .providers import ToolError
        with self._lock, self._db() as db:
            activities = [json.loads(row[0]) for row in db.execute("SELECT data FROM activities ORDER BY rowid DESC LIMIT 50")]
        for activity in activities:
            for key in ("title", "detail"):
                try:
                    validate_public_business_text(activity.get(key, ""))
                except ToolError:
                    activity[key] = "后台维护记录" if key == "title" else "后台资料已更新；内部技术信息不在业务界面展示。"
        return activities

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

    def projects(self) -> list[dict]:
        from .business_projects import BusinessProjects
        return BusinessProjects(self).list()

    def get_project(self, project_id: str) -> dict:
        from .business_projects import BusinessProjects
        return BusinessProjects(self).get(project_id)

    def create_project(self, name: str, description: str, repository_ids: list[str], repository_roles: dict | None = None) -> dict:
        from .business_projects import BusinessProjects
        return BusinessProjects(self).create(name, description, repository_ids, repository_roles)

    def update_project(self, project_id: str, revision: str, **changes) -> dict:
        from .business_projects import BusinessProjects
        return BusinessProjects(self).update(project_id, revision, **changes)

    def project_for_repository(self, repository_id: str) -> dict | None:
        from .business_projects import BusinessProjects
        return BusinessProjects(self).for_repository(repository_id)

    def workspace(self) -> dict:
        graph = self.graph()
        repos = self.repositories()
        projects = self.projects()
        from .content_layout import is_source_document
        with self._lock:
            note_count = sum(self.public_business_note(note) for note in self._cache.values())
            source_count = 0
        return {"name": "业务知识库", "vault_path": str(self.vault), "version": self.version,
                "demo": self.config_get("demo_vault_path") == str(self.vault),
                "stats": {"notes": note_count, "business_notes": note_count - source_count,
                          "source_documents": source_count, "links": len(graph["edges"]), "repositories": len(repos), "projects": len(projects)},
                "projects": projects, "repositories": repos, "recent_activity": self.activities()[:8]}

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
                files = all_files
                previous_index = self.config_get(f"repository_file_index:{repo_id}", {})
                previous = {item["path"]: item["blob"] for item in previous_index.get("files", [])}
                current = {item["path"]: item["blob"] for item in files}
                changed_paths = sorted(name for name in previous.keys() | current.keys() if previous.get(name) != current.get(name))
                self.update_task(task_id, 35, "正在更新代码文件索引（未调用 AI）")
                self._publish_code_index(repo, commit, files, len(all_files), task_id)
                self.activity("后台资料已更新", "业务知识将在主动分析完成后更新。", "success")
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
        """Store source discovery metadata privately; Git sync never publishes Markdown."""
        vault = Path(repo["vault_path"]).resolve()
        with self._lock:
            if self.vault != vault:
                raise CoreError(409, "知识库已切换，请重新发起仓库同步")
            self.config_set(f"repository_file_index:{repo['id']}", {
                "vault_path": str(vault), "commit": commit, "files": files,
                "total_files": total, "updated_at": now()})
        if task_id:
            self.update_task(task_id, 90, f"已更新 {len(files)} 个文件的私有索引；未发布业务文档")

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
