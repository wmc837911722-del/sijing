"""Explicit, vault-scoped business projects above independent Git repositories."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from .core import CoreError


class BusinessProjects:
    KEY = "business_projects"

    def __init__(self, store):
        self.store = store

    def list(self):
        vault = str(self.store.vault)
        return [project for project in self.store.config_get(self.KEY, []) if project.get("vault_path") == vault]

    def get(self, project_id):
        project = next((project for project in self.list() if project["id"] == project_id), None)
        if project is None:
            raise CoreError(404, "业务项目不存在于当前知识库")
        return project

    def for_repository(self, repository_id):
        return next((project for project in self.list() if repository_id in project["repository_ids"]), None)

    @staticmethod
    def _text(name, description):
        if not isinstance(name, str) or not name.strip() or len(name) > 160 or any(ord(char) < 32 for char in name):
            raise CoreError(400, "请填写有效的业务项目名称（最多160字）")
        if not isinstance(description, str) or len(description) > 4000:
            raise CoreError(400, "业务项目说明最多4000字")
        return name.strip(), description.strip()

    def _members(self, repository_ids, roles):
        if (not isinstance(repository_ids, list) or not 1 <= len(repository_ids) <= 100
                or any(not isinstance(value, str) or not value for value in repository_ids)
                or len(set(repository_ids)) != len(repository_ids)):
            raise CoreError(400, "业务项目需包含1–100个不重复的仓库")
        for repository_id in repository_ids:
            self.store.get_repository(repository_id)
        if not isinstance(roles, dict) or any(key not in repository_ids or value not in {"frontend", "backend"} for key, value in roles.items()):
            raise CoreError(400, "仓库角色必须对应项目成员，并选择前端或后端")
        return list(repository_ids), dict(roles)

    @staticmethod
    def _ownership(projects, repository_ids, vault, project_id=None):
        for project in projects:
            if project.get("vault_path") == vault and project["id"] != project_id:
                if set(project["repository_ids"]).intersection(repository_ids):
                    raise CoreError(409, "仓库已属于其他业务项目，请先从原项目移除")

    def create(self, name, description, repository_ids, repository_roles=None):
        name, description = self._text(name, description)
        repository_ids, roles = self._members(repository_ids, repository_roles or {})
        vault = str(self.store.vault)
        project = {"id": uuid.uuid4().hex, "name": name, "description": description,
                   "repository_ids": repository_ids, "repository_roles": roles,
                   "revision": uuid.uuid4().hex, "vault_path": vault}
        def add(projects):
            if str(self.store.vault) != vault:
                raise CoreError(409, "知识库已切换，请重新创建业务项目")
            self._members(repository_ids, roles)
            self._ownership(projects or [], repository_ids, vault)
            return [*(projects or []), project]
        self.store.config_update(self.KEY, add, [])
        return project

    def update(self, project_id, expected_revision, *, name=None, description=None, repository_ids=None, repository_roles=None):
        vault = str(self.store.vault)
        result = None
        def update(projects):
            nonlocal result
            if str(self.store.vault) != vault:
                raise CoreError(409, "知识库已切换，请重新读取业务项目")
            current = next((project for project in (projects or []) if project["id"] == project_id and project.get("vault_path") == vault), None)
            if current is None:
                raise CoreError(404, "业务项目不存在于当前知识库")
            if not isinstance(expected_revision, str) or current["revision"] != expected_revision:
                raise CoreError(409, "业务项目已变化，请重新读取后修改")
            members = list(current["repository_ids"] if repository_ids is None else repository_ids)
            roles = ({key: value for key, value in current.get("repository_roles", {}).items() if key in members}
                     if repository_roles is None else repository_roles)
            members, roles = self._members(members, roles)
            chosen_name, chosen_description = self._text(current["name"] if name is None else name,
                                                        current["description"] if description is None else description)
            self._ownership(projects or [], members, vault, project_id)
            result = {**current, "name": chosen_name, "description": chosen_description,
                      "repository_ids": members, "repository_roles": roles, "revision": uuid.uuid4().hex}
            return [result if project["id"] == project_id else project for project in projects]
        self.store.config_update(self.KEY, update, [])
        return result

    def import_repositories(self, name, description, frontend, backend):
        """Validate the entire form before creating any repository or starting Git jobs."""
        name, description = self._text(name, description)
        if not frontend or not backend or len(frontend) + len(backend) > 100:
            raise CoreError(400, "请分别填写至少一个前端仓库和一个后端仓库（合计最多100个）")
        vault = str(self.store.vault)
        prepared, seen = [], set()
        for role, inputs in (("frontend", frontend), ("backend", backend)):
            for value in inputs:
                if not isinstance(value, dict) or not isinstance(value.get("url"), str):
                    raise CoreError(400, "仓库地址无效")
                url, remote = self.store.git_transport.endpoint(value["url"])
                if url in seen:
                    raise CoreError(400, "前端和后端仓库地址不能重复")
                seen.add(url)
                connection = value.get("connection")
                normalized = self.store.git_transport.normalize(url, connection)
                self.store.git_transport.resolve_secret(normalized, connection)
                branch = self.store.git_transport.branch(value.get("branch", "")) if remote else ""
                display = value.get("name")
                if display is not None and (not isinstance(display, str) or len(display) > 160):
                    raise CoreError(400, "仓库名称无效")
                if not remote:
                    local = Path(url).expanduser().resolve()
                    if not local.is_dir():
                        raise CoreError(400, "本地仓库目录不存在")
                    self.store._git(["rev-parse", "--git-dir"], local)
                prepared.append((role, {"url": url, "name": display, "branch": branch, "connection": connection}))
        created, roles, project = [], {}, None
        # Keep the vault stable until all imports have a persisted business owner.
        # Actual clone/sync tasks remain asynchronous, with their existing retry/error UI.
        with self.store._lock:
            if str(self.store.vault) != vault:
                raise CoreError(409, "知识库已切换，请重新导入业务项目")
            try:
                for role, value in prepared:
                    repository = self.store.add_repository(**value)
                    created.append(repository["id"])
                    roles[repository["id"]] = role
            finally:
                # A credential-store/filesystem failure after the first import
                # must leave its source and business owner reviewable, never orphan it.
                if created:
                    project = self.create(name, description, created, roles)
        return project
