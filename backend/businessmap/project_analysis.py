"""One bounded business analysis over explicitly grouped repository snapshots."""
from __future__ import annotations

import json
import posixpath
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

from .analysis_output import AnalysisOutputError, normalize_markdown, prepare_file, verify_sources
from .business_projects import BusinessProjects
from .core import CoreError, now
from .providers import ToolError, parse_json_answer, provider_destination


PROJECT_SYSTEM = """
本次分析对象是一个业务项目，frontend/backend是用户指定的仓库角色，不是业务结论。
repositories列出本项目各仓库的名称、角色、固定提交及实际覆盖；files.path以repository_id/真实路径组成，防止不同仓库同名文件混淆。
从已提供的前端页面、请求契约与后端实现共同归纳一个业务主题，不能分别给每个仓库生成孤立业务地图。
只有两端均有实际来源并且接口路由、请求方法/字段或调用实现能逐项对应时，才能声称前端调用某后端或形成端到端业务流程；同名词或同在一个项目不构成连线证据。
未读到的跨端接口或业务链路必须明确写为待确认；导入了全部成员仓库不等于已读取所有代码。
输出同一套项目实体、流程、规则、数据卡与业务地图；不要把仓库名、文件名列表当成业务地图。
"""


def _note_path_key(name):
    return unicodedata.normalize("NFC", posixpath.normpath(name)).casefold()


def _other_projects_note_paths(db, state_key, vault, involved):
    rows = db.execute("SELECT value FROM config WHERE key GLOB 'project_analysis:*' AND key<>?", (state_key,))
    occupied = set()
    for (raw,) in rows:
        state = json.loads(raw)
        location = state.get("vault_path")
        if location and _note_path_key(str(Path(location).resolve())) == _note_path_key(str(vault.resolve())):
            occupied.update(_note_path_key(note["path"]) for note in state.get("notes", {}).values() if note.get("path"))
    return occupied & involved


class ProjectAnalysis:
    KEYS = ("overview", "entities", "flows", "rules", "data")

    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.projects = BusinessProjects(self.store)

    def model_policy(self, project_id):
        project = self.projects.get(project_id)
        policies = [{"repository_id": repository_id, **self.service.model_policy(repository_id)}
                    for repository_id in project["repository_ids"]]
        states = {policy["status"] for policy in policies}
        if "missing" in states:
            status = "missing"
        elif "unbound" in states:
            status = "unbound"
        elif "changed" in states:
            status = "changed"
        else:
            destinations = {(policy.get("provider_id"), policy.get("kind"), policy.get("base_url"), policy.get("model")) for policy in policies}
            status = "ready" if len(destinations) == 1 else "changed"
        common = {key: policies[0][key] for key in ("provider_id", "provider_name", "kind", "base_url", "model")
                  if status == "ready" and key in policies[0]}
        summaries = {"ready": "所有成员仓库已授权同一模型目标，可主动联合分析；自动AI更新保持关闭。",
                     "unbound": "部分成员仓库尚未授权模型，请为整个业务项目显式绑定。",
                     "changed": "成员模型目标不一致或配置已变化，请为整个业务项目重新绑定。",
                     "missing": "项目成员绑定的模型已删除，请重新选择并绑定。"}
        return {**common, "status": status, "description": summaries[status], "repositories": policies}

    def bind_model(self, project_id, provider_id):
        project = self.projects.get(project_id)
        provider = self.service.providers.get(provider_id) if provider_id else None
        if not provider:
            raise ToolError("请选择要授权给此业务项目全部仓库的模型配置。", 404)
        vault = str(self.store.vault)
        with self.store._lock, self.store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.projects.get(project_id)
            if str(self.store.vault) != vault or current["revision"] != project["revision"]:
                raise ToolError("业务项目已变化，请重新读取后绑定模型。", 409)
            selected = self.service.providers.get(provider_id)
            if not selected or provider_destination(selected) != provider_destination(provider):
                raise ToolError("模型目标已变化，请重新读取并绑定。", 409)
            for repository_id in project["repository_ids"]:
                self.store.get_repository(repository_id)
                policy = {"provider_id": provider["id"], "provider_name": provider["name"],
                          **{key: provider[key] for key in ("kind", "base_url", "model")}, "revision": uuid.uuid4().hex}
                self._set(db, f"repository_model_policy:{repository_id}", policy)
                row = db.execute("SELECT value FROM config WHERE key=?", (f"ai_analysis:{repository_id}",)).fetchone()
                self._set(db, f"ai_analysis:{repository_id}", {**(json.loads(row[0]) if row else {}), "auto_enabled": False})
        self.store.activity("已绑定业务项目模型", "已为全部成员仓库显式授权同一目标；自动AI更新关闭。绑定本身不发送源码。", "info")
        return self.model_policy(project_id)

    @staticmethod
    def _set(db, key, value):
        db.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def snapshot(self, project_id):
        project = self.projects.get(project_id)
        roles = {project.get("repository_roles", {}).get(repository_id) for repository_id in project["repository_ids"]}
        if not {"frontend", "backend"}.issubset(roles):
            raise ToolError("请补齐前端和后端仓库后再联合分析", 409)
        if self.model_policy(project_id)["status"] != "ready":
            raise ToolError("请先为业务项目全部成员绑定同一模型目标。", 409)
        repositories, policies, provider = [], {}, None
        for repository_id in project["repository_ids"]:
            repository = self.store.get_repository(repository_id)
            if repository.get("status") != "ready" or not repository.get("commit"):
                raise ToolError("项目成员仓库尚未全部完成同步，请完成后再联合分析。", 409)
            repositories.append({key: repository.get(key) for key in ("id", "name", "commit", "path", "vault_path", "status")})
            policies[repository_id] = self.store.config_get(f"repository_model_policy:{repository_id}")
            selected = self.service.repository_provider(repository_id)
            if provider is not None and provider_destination(selected) != provider_destination(provider):
                raise ToolError("项目成员未授权相同模型目标，请重新绑定。", 409)
            provider = selected
        return {"project": project, "repositories": repositories, "policies": policies,
                "provider": provider, "vault_path": str(self.store.vault), "vault_epoch": self.store._vault_epoch}

    def guard(self, snapshot):
        try:
            if str(self.store.vault) != snapshot["vault_path"] or self.store._vault_epoch != snapshot["vault_epoch"]:
                raise ToolError("知识库已变化。", 409)
            # Read one consistent local snapshot. Per-batch/per-poll guards must
            # not open dozens of SQLite connections or retrieve model secrets.
            ids = [repository["id"] for repository in snapshot["repositories"]]
            keys = ["business_projects", "providers", "vault_path", *("repository_model_policy:" + rid for rid in ids)]
            with self.store._lock, self.store._db() as db:
                db.execute("BEGIN")
                config = {row[0]: json.loads(row[1]) for row in db.execute(
                    "SELECT key,value FROM config WHERE key IN (" + ",".join("?" for _ in keys) + ")", keys)}
                repositories = {row[0]: json.loads(row[1]) for row in db.execute(
                    "SELECT id,data FROM repositories WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)}
            project = next((value for value in config.get("business_projects", [])
                            if value["id"] == snapshot["project"]["id"] and value.get("vault_path") == snapshot["vault_path"]), None)
            if (config.get("vault_path") != snapshot["vault_path"] or not project or project["revision"] != snapshot["project"]["revision"]
                    or project["repository_ids"] != snapshot["project"]["repository_ids"]):
                raise ToolError("项目范围已变化。", 409)
            provider = next((value for value in config.get("providers", []) if value["id"] == snapshot["provider"]["id"]), None)
            if not provider or provider_destination(provider) != provider_destination(snapshot["provider"]):
                raise ToolError("模型目标已变化。", 409)
            for repository in snapshot["repositories"]:
                current = repositories.get(repository["id"])
                if not current or any(current.get(key) != repository.get(key) for key in ("commit", "path", "vault_path", "status")):
                    raise ToolError("代码版本已变化。", 409)
                if config.get(f"repository_model_policy:{repository['id']}") != snapshot["policies"][repository["id"]]:
                    raise ToolError("模型授权已变化。", 409)
        except (CoreError, ToolError):
            raise ToolError("分析期间业务项目、代码版本、知识库或模型授权发生变化，已拒绝过时结果，请重新分析。", 409) from None

    def status(self, project_id):
        self.projects.get(project_id)
        state = self.store.config_get(f"project_analysis_status:{project_id}", {})
        if state.get("vault_path") != str(self.store.vault):
            state = {}
        return {"status": state.get("status", "never"), **{key: state[key] for key in
                ("summary", "category", "updated_at", "project_revision", "scope", "source_repositories") if key in state}}

    @contextmanager
    def _status(self, project_id, snapshot):
        key = f"project_analysis_status:{project_id}"
        state = {"status": "running", "summary": "正在联合分析项目各端的有界代码片段。", "updated_at": now(),
                 "vault_path": snapshot["vault_path"], "project_revision": snapshot["project"]["revision"],
                 "source_repositories": self._sources(snapshot)}
        self.store.config_set(key, state)
        try:
            yield state
        except Exception as error:
            category = getattr(error, "category", None) or ("changed" if getattr(error, "status_code", None) == 409
                        else "model" if isinstance(error, ToolError) else "internal")
            state.update(status="failed", category=category, updated_at=now(), summary={
                "citation": "代码引用未通过核验，原有项目知识保留。",
                "format": "模型未返回有效项目知识，原有内容保留。",
                "changed": "分析期间项目范围、源码或授权发生变化，已拒绝过时结果。",
                "model": "模型调用未获得有效结果，请检查连接或额度。",
                "internal": "联合分析未完成，请检查本地服务后重试。"}.get(category, "联合分析未完成，原有内容保留。"))
            self.store.config_set(key, state)
            raise
        else:
            state.update(status="completed", updated_at=now())
            self.store.config_set(key, state)

    @staticmethod
    def _sources(snapshot):
        return [{"repository_id": repository["id"], "commit": repository["commit"]} for repository in snapshot["repositories"]]

    def _sample(self, snapshot):
        contexts = []
        for repository in snapshot["repositories"]:
            context = self.store.repository_context(repository["id"])
            if context["commit"] != repository["commit"]:
                raise ToolError("采样期间代码版本变化，请重试。", 409)
            contexts.append(context)
        count = len(contexts)
        char_budget = [29000 // count] * count
        wire_budget = [37000 // count] * count
        selected, identities = [], {}
        for index in range(max((len(context["files"]) for context in contexts), default=0)):
            for member, context in enumerate(contexts):
                if index >= len(context["files"]) or char_budget[member] <= 0 or wire_budget[member] <= 0:
                    continue
                item = context["files"][index]
                if not item["content"].strip():
                    continue
                repository_id = context["repository"]["id"]
                path = repository_id + "/" + item["path"]
                prepared, wire_size = prepare_file({**item, "path": path}, min(6000, char_budget[member]), wire_budget[member])
                if prepared is None:
                    continue
                selected.append(prepared)
                identities[path] = {"repository_id": repository_id, "path": item["path"], "commit": context["commit"]}
                char_budget[member] -= len(prepared["content"])
                wire_budget[member] -= wire_size + 2
        if not selected:
            raise ToolError("项目没有可安全分析的源代码片段。")
        coverage = []
        for context in contexts:
            repository_id = context["repository"]["id"]
            provided = [item for item in selected if identities[item["path"]]["repository_id"] == repository_id]
            total = context.get("total_files", len(context["files"]))
            coverage.append({"repository_id": repository_id, "name": context["repository"]["name"],
                             "role": snapshot["project"].get("repository_roles", {}).get(repository_id),
                             "commit": context["commit"], "files": len(provided), "total_files": total,
                             "line_count": sum(item["line_count"] for item in provided),
                             "truncated": bool(context.get("truncated") or len(provided) < total or any(item["truncated"] for item in provided))})
        return selected, identities, {"files": len(selected), "line_count": sum(item["line_count"] for item in selected),
                                      "total_files": sum(item["total_files"] for item in coverage),
                                      "truncated": any(item["truncated"] for item in coverage), "repositories": coverage}

    def queue(self, project_id):
        snapshot = self.snapshot(project_id)
        self.guard(snapshot)
        with self.store._lock:
            for task in self.store.tasks():
                if (task["kind"] == "project-analysis" and task.get("payload", {}).get("project_id") == project_id
                        and task["status"] in {"queued", "running"}):
                    return {"task_id": task["id"], "summary": "业务项目联合分析已在队列中。"}
            task = self.store.create_task("project-analysis", {"project_id": project_id, "project_revision": snapshot["project"]["revision"]})
            def run(task_id):
                result = self.analyze(project_id, task_id=task_id, expected_snapshot=snapshot)
                status = result.get("status") if isinstance(result, dict) else None
                # The generic executor treats a normal return as job success.
                # A resumable analysis may return a failed/paused result, so
                # translate its domain outcome before the executor finishes.
                if status in {"failed", "blocked"}:
                    raise ToolError("业务项目完整分析未完成；有效断点已保留，请查看完整分析状态后继续。", 422)
                if status == "paused":
                    message = "完整分析已暂停；有效断点已保存，未完成批次可稍后继续。"
                elif status == "cancelled":
                    message = "完整分析已取消；有效断点与已有文档已保留。"
                elif status == "completed" and result.get("publication_complete") is False:
                    message = "代码分析完成，文档待合并；人工修改已保留，请处理更新建议。"
                elif status == "completed" and result.get("publication_complete") is True:
                    message = "业务项目完整分析及文档保存完成。"
                else:
                    raise ToolError("完整分析未返回可确认的结束状态；请查看完整分析状态。", 502)
                self.store.update_task(task_id, 100, message)
                return result
            self.store.run_task(task["id"], run)
        return {"task_id": task["id"], "summary": "已排队联合分析前后端业务；将按各仓库固定版本逐批完整分析所有安全源码。"}

    def analyze(self, project_id, task_id=None, expected_snapshot=None):
        from .full_analysis import FullProjectAnalysis
        return FullProjectAnalysis(self.service).analyze(project_id, task_id=task_id, expected_snapshot=expected_snapshot)

    def analyze_sampled(self, project_id, task_id=None, expected_snapshot=None):
        from .content_layout import CATEGORY_NAMES, public_project_paths, public_topic_path
        from .tools_api import ANALYSIS_SYSTEM, _ANALYSIS_LOCK
        snapshot = expected_snapshot or self.snapshot(project_id)
        with _ANALYSIS_LOCK, self._status(project_id, snapshot) as status:
            self.guard(snapshot)
            project = snapshot["project"]
            selected, identities, scope = self._sample(snapshot)
            status["scope"] = scope
            self.store.config_set(f"project_analysis_status:{project_id}", status)
            paths = {**public_project_paths(project["name"]),
                     **{key: public_topic_path(project["name"], key, label) for key, label in CATEGORY_NAMES.items()}}
            self.store.refresh()
            # Titles arrive from the model. Freeze existing path revisions now,
            # so a concurrent note at a newly chosen title is never overwritten.
            with self.store._lock:
                before_paths = {path: dict(note) for path, note in self.store._cache.items()}
            state = self.store.config_get(f"project_analysis:{project_id}", {})
            if state.get("vault_path") != snapshot["vault_path"]:
                state = {}
            system = ANALYSIS_SYSTEM + PROJECT_SYSTEM
            prompt = json.dumps({"project": {key: project[key] for key in ("id", "name", "description", "repository_roles")},
                                 "repositories": scope["repositories"], "scope_limited": scope["truncated"],
                                 "coverage": scope, "note_paths": paths,
                                 "files": [{key: item["numbered_content"] if key == "content" else item["evidence_metadata"] if key == "evidence" else item[key]
                                            for key in ("path", "content", "line_count", "truncated", "evidence", "evidence_uncovered_lines")} for item in selected]}, ensure_ascii=False)
            if len(prompt) + len(system) > 44000:
                raise AnalysisOutputError("项目分析上下文超过安全上限，请缩小项目范围。", "format")
            self.guard(snapshot)
            if task_id:
                self.store.update_task(task_id, 25, "正在联合归纳前后端业务；导入全部仓库不等于已读完全部代码")
            response = self.service.providers.complete(system, prompt, provider_id=snapshot["provider"]["id"],
                max_output=6000, expected_destination=provider_destination(snapshot["provider"]), before_send=lambda: self.guard(snapshot))
            try:
                result = parse_json_answer(response["text"])
            except ToolError:
                raise AnalysisOutputError("模型未返回有效JSON，未写入。", "format") from None
            if not isinstance(result, dict) or not isinstance(result.get("notes"), list) or not 1 <= len(result["notes"]) <= 5:
                raise AnalysisOutputError("模型未返回1–5篇有效的项目业务笔记，未写入。", "format")
            prepared = {}
            for note in result["notes"]:
                if (not isinstance(note, dict) or note.get("key") not in self.KEYS or note["key"] in prepared
                        or not isinstance(note.get("title"), str) or not note["title"].strip()
                        or not isinstance(note.get("content"), str) or not note["content"].strip()
                        or not isinstance(note.get("sources"), list) or not note["sources"]):
                    raise AnalysisOutputError("模型项目笔记缺少有效标识、正文或代码依据，未写入。", "format")
                checked = verify_sources(note["sources"], selected, note_key=note["key"])
                sources = [{**source, **identities[source["path"]]} for source in checked]
                prepared[note["key"]] = {"title": note["title"][:160], "body": normalize_markdown(note["content"]).strip(),
                                          "sources": sources}
            for key, note in prepared.items():
                if key != "overview":
                    paths[key] = public_topic_path(project["name"], key, note["title"])
            output_paths = dict(paths)
            before = {key: before_paths.get(path) for key, path in output_paths.items()}
            self.guard(snapshot)
            result = self._publish(snapshot, state, paths, output_paths, before, prepared, scope)
            result.update(usage=response.get("usage", {}), provider=response.get("provider"))
            status["summary"] = result["summary"]
            if task_id:
                self.store.update_task(task_id, 95, result["summary"])
            self.store.activity("业务项目联合分析完成", result["summary"], "warning" if result["conflicts"] else "success")
            return result

    def _document(self, snapshot, scope, key, title, body, sources):
        from .business_content import render_public_note, validate_public_business_text
        role = "business_map" if key == "overview" else "analysis_summary" if key == "summary" else "business_knowledge"
        private_sources = list(dict.fromkeys(source["repository_id"] + "/" + source["path"] for source in sources))
        forbidden = [snapshot["project"]["id"], *private_sources]
        for source in sources:
            forbidden.extend(source.get(field, "") for field in ("repository_id", "commit", "path"))
            forbidden.extend(source.get("commit", "")[:size] for size in (7, 8, 10, 12))
        validate_public_business_text(title + "\n" + body, forbidden)
        return render_public_note(title, body, content_role=role, tags=["业务知识"]), private_sources

    def _candidate(self, snapshot, key, path, content, current, source_paths):
        return {"id": "project-" + snapshot["project"]["id"] + "-" + key, "project_id": snapshot["project"]["id"],
                "project_revision": snapshot["project"]["revision"], "source_repositories": self._sources(snapshot),
                "policy_snapshot": snapshot["policies"], "key": key, "path": path, "content": content,
                "base_revision": current["revision"] if current else None, "source_paths": source_paths,
                "vault_path": snapshot["vault_path"], "created_at": now()}

    def _publish(self, snapshot, state, paths, output_paths, before, prepared, scope):
        from .layout_migration import _rewrite_links
        managed = {key: {**value, "stale": True} for key, value in state.get("notes", {}).items()}
        written, conflicts = {}, []
        created = updated = 0
        sources_snapshot = self._sources(snapshot)
        vault = Path(snapshot["vault_path"])
        # Validate all prospective public material before the first file write.
        for key, note in prepared.items():
            self._document(snapshot, scope, key, note["title"], note["body"], note["sources"])
        # SQLite's write lock prevents another local service process from changing
        # membership, commits or policy between the final guard and publication.
        with self.store._lock, self.store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(snapshot)
            row = db.execute("SELECT value FROM config WHERE key='project_analysis_candidates'").fetchone()
            candidates = json.loads(row[0]) if row else []

            def publish(key, content, source_paths):
                nonlocal created, updated, candidates
                current, old = before[key], state.get("notes", {}).get(key)
                conflict = bool(current and (not old or current["revision"] != old.get("revision")))
                if not conflict:
                    try:
                        saved_revision = self.store._write_note(output_paths[key], content, current["revision"] if current else None, vault=vault)
                    except CoreError as error:
                        if error.status_code != 409:
                            raise
                        conflict = True
                candidate_id = "project-" + snapshot["project"]["id"] + "-" + key
                candidates = [candidate for candidate in candidates if candidate["id"] != candidate_id]
                if conflict:
                    candidate = self._candidate(snapshot, key, output_paths[key], content, current, source_paths)
                    candidates.append(candidate)
                    conflicts.append({"id": candidate["id"], "path": candidate["path"]})
                    managed[key] = {**(old or {}), "path": output_paths[key], "stale": True, "conflict": True}
                    return False
                created += int(current is None)
                updated += int(current is not None)
                managed[key] = {"path": output_paths[key], "revision": saved_revision, "source_repositories": sources_snapshot,
                                "source_paths": source_paths, "stale": False, "conflict": False}
                return True

            for key in self.KEYS[1:]:
                if key in prepared:
                    note = prepared[key]
                    content, source_paths = self._document(snapshot, scope, key, note["title"], note["body"], note["sources"])
                    if publish(key, content, source_paths):
                        written[key] = note
            def link(key):
                title = written[key]["title"].replace("|", "／").replace("]", "）").replace("[", "（").replace("\n", " ")
                return f"[[{paths[key][:-3]}|{title}]]"
            navigation = "\n".join("- " + link(key) for key in written)
            all_sources = [source for note in written.values() for source in note["sources"]]
            overview = prepared.get("overview")
            allowed_links = {paths[key][:-3] for key in written}
            body = _rewrite_links(overview["body"], {}, replace_link=lambda match:
                match.group() if match[1].split("|", 1)[0].removesuffix(".md") in allowed_links else match[1].split("|", 1)[-1]) if overview else (
                    f"# {snapshot['project']['name']} · 业务地图\n\n按业务主题查阅相关知识；尚未核实的流程和关系保留为待确认。")
            body += "\n\n## 业务知识导航\n\n" + (navigation or "本次没有成功保存的业务知识卡；更新建议不计入已确认知识。")
            map_sources = [*all_sources, *(overview["sources"] if overview else [])]
            content, source_paths = self._document(snapshot, scope, "overview", overview["title"] if overview else snapshot["project"]["name"] + " · 业务地图", body, map_sources)
            map_saved = publish("overview", content, source_paths)
            summary = f"# {snapshot['project']['name']} · 业务梳理总结\n\n本次整理范围有限，未确认的业务内容不能据此推断。\n\n## 本次已保存知识\n\n" + (navigation or "无成功保存卡片。")
            if map_saved:
                summary += f"\n\n业务地图：[[{paths['overview'][:-3]}|业务地图]]。"
            summary += "\n\n## 待确认\n\n- 业务之间的关系需分别核实；同名或属于同一项目不能证明业务闭环。\n- 文档归纳不代替业务实际运行与验收。"
            missing = [key for key in self.KEYS[1:] if key not in written]
            if missing:
                from .content_layout import CATEGORY_NAMES
                summary += "\n- 本次未保存主题：" + "、".join(CATEGORY_NAMES[key] for key in missing) + "；不能据此认定业务不存在。"
            if conflicts:
                summary += f"\n- {len(conflicts)} 篇文档存在人工修改或并发变化，已保留并建立独立更新建议。"
            content, source_paths = self._document(snapshot, scope, "summary", snapshot["project"]["name"] + " · 分析总结", summary, map_sources)
            summary_saved = publish("summary", content, source_paths)
            self._set(db, f"project_analysis:{snapshot['project']['id']}", {"vault_path": snapshot["vault_path"],
                "project_revision": snapshot["project"]["revision"], "source_repositories": sources_snapshot,
                "notes": managed, "scope_limited": scope["truncated"], "auto_enabled": False})
            self._set(db, "project_analysis_candidates", candidates)
        self.store.refresh()
        summary = (f"已保存 {len(written)} 篇项目业务知识，业务地图{'已保存' if map_saved else '已建立更新建议'}，"
                   f"总结{'已保存' if summary_saved else '已建立更新建议'}；本次读取 {scope['files']} / {scope['total_files']} 个安全文件、"
                   f"{scope['line_count']} 行，各端为有界采样；自动AI更新关闭。")
        if conflicts:
            summary += f" {len(conflicts)} 篇人工修改或并发变更已保留。"
        return {"notes_created": created, "notes_updated": updated, "conflicts": conflicts, "summary": summary, "scope": scope}

    def candidates(self):
        return [candidate for candidate in self.store.config_get("project_analysis_candidates", []) if candidate.get("vault_path") == str(self.store.vault)]

    def apply_candidate(self, candidate_id, revision):
        candidate = next((candidate for candidate in self.candidates() if candidate["id"] == candidate_id), None)
        if not candidate:
            raise ToolError("项目更新建议不存在。", 404)
        snapshot = self.snapshot(candidate["project_id"])
        if (snapshot["project"]["revision"] != candidate["project_revision"] or self._sources(snapshot) != candidate["source_repositories"]
                or snapshot["policies"] != candidate["policy_snapshot"]):
            raise ToolError("项目范围、源码或模型授权已变化，此建议已过期，请重新分析。", 409)
        with self.store._lock, self.store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(snapshot)
            row = db.execute("SELECT value FROM config WHERE key='project_analysis_candidates'").fetchone()
            candidates = json.loads(row[0]) if row else []
            current = next((item for item in candidates if item["id"] == candidate_id), None)
            if current != candidate:
                raise ToolError("项目更新建议已变化，请重新读取后确认应用。", 409)
            state_key = f"project_analysis:{candidate['project_id']}"
            row = db.execute("SELECT value FROM config WHERE key=?", (state_key,)).fetchone()
            state = json.loads(row[0]) if row else {}
            vault = Path(snapshot["vault_path"])
            if previous_paths := candidate.get("relocation_from"):
                if isinstance(previous_paths, str):
                    previous_paths = [previous_paths]
                sources = [self.store._path(name, vault=vault) for name in previous_paths]
                target = _note_path_key(candidate["path"])
                other_keys = {_note_path_key(note["path"]) for key, note in state.get("notes", {}).items()
                              if key != candidate["key"] and note.get("path")}
                if (any(path.exists() or path.is_symlink() for path in sources) or target in other_keys
                        or _other_projects_note_paths(db, state_key, vault, {target})):
                    raise ToolError("原文档或目标位置仍被使用，已保留现有文件。处理文件冲突后可重新分析。", 409)
            saved_revision = self.store._write_note(candidate["path"], candidate["content"], revision, vault=vault)
            state.update(vault_path=snapshot["vault_path"], project_revision=snapshot["project"]["revision"], source_repositories=self._sources(snapshot))
            state.setdefault("notes", {})[candidate["key"]] = {"path": candidate["path"], "revision": saved_revision,
                "source_repositories": candidate["source_repositories"], "source_paths": candidate["source_paths"], "stale": False, "conflict": False}
            self._set(db, f"project_analysis:{candidate['project_id']}", state)
            self._set(db, "project_analysis_candidates", [item for item in candidates if item["id"] != candidate_id])
        self.store.refresh()
        return self.store.read_note(candidate["path"])
