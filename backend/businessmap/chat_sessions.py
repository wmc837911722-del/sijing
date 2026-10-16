"""Persisted local conversations with server-owned, bounded model history."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
import re
import uuid

from .providers import ToolError


MAX_MESSAGES = 200
MAX_ACTIVITIES = 80


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class ChatSessions:
    def __init__(self, store, service):
        self.store = store
        self.service = service

    def _key(self, chat_id):
        if not re.fullmatch(r"[a-f0-9]{32}", chat_id):
            raise ToolError("会话不存在。", 404)
        return f"chat:{chat_id}"

    @contextmanager
    def _lock(self, chat_id):
        # Kernel locks survive neither process crashes nor restarts. They avoid
        # timeout leases admitting a second query while the first is still alive.
        self._key(chat_id)
        directory = self.store.data_dir / "chat-locks"
        directory.mkdir(exist_ok=True)
        with (directory / f"{chat_id}.lock").open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ToolError("此会话正在生成回复，请等待完成。", 409) from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self, chat_id):
        session = self.store.config_get(self._key(chat_id))
        if not session or session.get("_vault_path") != str(self.store.vault):
            raise ToolError("会话不存在于当前知识库。", 404)
        return session

    @staticmethod
    def _public(session):
        result = {key: value for key, value in session.items() if not key.startswith("_")}
        result["messages"] = [{key: value for key, value in message.items() if not key.startswith("_")}
                              for message in session["messages"]]
        return result

    @staticmethod
    def _summary(session):
        result = {key: session.get(key) for key in (
            "id", "title", "created_at", "updated_at", "mode", "provider_id", "repository_id", "datasource_id", "pending")}
        result["message_count"] = len(session["messages"])
        result["preview"] = session["messages"][-1]["content"][:120] if session["messages"] else ""
        return result

    def _save(self, session):
        excess = max(0, len(session["messages"]) - MAX_MESSAGES)
        if excess:
            session["messages"] = session["messages"][excess:]
            session["earlier_messages_removed"] += excess
        self.store.config_set(self._key(session["id"]), session)
        summary = {**self._summary(session), "_vault_path": session["_vault_path"]}
        self.store.config_update("chat_index", lambda old: [s for s in old if s["id"] != session["id"]] + [summary], [])

    def create(self, title=None):
        title = (title or "").strip()
        if len(title) > 160:
            raise ToolError("会话标题过长。")
        session = {"id": uuid.uuid4().hex, "title": title or "新对话", "created_at": timestamp(),
                   "updated_at": timestamp(), "messages": [], "mode": "knowledge", "provider_id": None,
                   "repository_id": None, "datasource_id": None, "pending": False, "pending_activity": [],
                   "cancel_requested": False, "earlier_messages_removed": 0,
                   "_vault_path": str(self.store.vault), "_automatic_title": not bool(title)}
        self._save(session)
        return self._public(session)

    def list(self):
        vault = str(self.store.vault)
        entries = [entry for entry in self.store.config_get("chat_index", []) if entry.get("_vault_path") == vault]
        # A crash can leave an unanswered user message. Only recover it after
        # obtaining the same OS lock used by an actual live request.
        for entry in entries:
            if entry.get("pending"):
                self.get(entry["id"])
        entries = [entry for entry in self.store.config_get("chat_index", []) if entry.get("_vault_path") == vault]
        return [{key: value for key, value in entry.items() if not key.startswith("_")}
                for entry in sorted(entries, key=lambda item: item["updated_at"], reverse=True)]

    def _recover(self, session):
        if session.get("pending"):
            error = "上次回复因应用中断而未完成；原问题已保留，请重新发送。"
            session["messages"].append({"id": uuid.uuid4().hex, "role": "assistant", "content": error, "error": error,
                                        "activities": self._finished_activities(session.get("pending_activity", [])),
                                        "completion": "limited"})
            session["pending"] = False
            session["pending_activity"] = []
            session["cancel_requested"] = False
            session["updated_at"] = timestamp()
            self._save(session)
        return session

    def get(self, chat_id):
        session = self._read(chat_id)
        if session.get("pending"):
            try:
                with self._lock(chat_id):
                    session = self._recover(self._read(chat_id))
            except ToolError as exc:
                if exc.status_code != 409:
                    raise
        return self._public(session)

    @staticmethod
    def _finished_activities(activities):
        return [{**item, "status": "error", "detail": "此步骤中断，未完成。"}
                if item.get("status") == "running" else item for item in activities]

    def cancel(self, chat_id):
        self._read(chat_id)

        def request_stop(session):
            if not session or session.get("_vault_path") != str(self.store.vault):
                raise ToolError("会话不存在于当前知识库。", 404)
            if session.get("pending"):
                session["cancel_requested"] = True
                session["updated_at"] = timestamp()
            return session

        # Cancellation cannot acquire the long-held send lock. Merge only this
        # flag in a SQLite transaction so concurrent progress cannot erase it.
        return self._public(self.store.config_update(self._key(chat_id), request_stop))

    def send(self, chat_id, message, mode="knowledge", provider_id=None, repository_id=None, datasource_id=None):
        self._read(chat_id)
        if mode not in ("knowledge", "data") or not isinstance(message, str) or not message.strip() or len(message) > 8000:
            raise ToolError("请提供有效的问题和模式。")
        with self._lock(chat_id):
            session = self._recover(self._read(chat_id))
            self.service.validate_selections(provider_id, repository_id, datasource_id)
            scope = self.service.history_scope(mode, repository_id, datasource_id, provider_id)
            history = []
            for previous in reversed(session["messages"]):
                if previous.get("_scope") != scope or previous.get("error"):
                    break
                history.append({"role": previous["role"], "content": previous["content"][:2000]})
                if len(history) == 8:
                    break
            history.reverse()
            if session["_automatic_title"] and not session["messages"]:
                session["title"] = re.sub(r"\s+", " ", message).strip()[:60]
            session.update(mode=mode, provider_id=provider_id, repository_id=repository_id,
                           datasource_id=datasource_id, pending=True, pending_activity=[],
                           cancel_requested=False, updated_at=timestamp())
            session["messages"].append({"id": uuid.uuid4().hex, "role": "user", "content": message, "_scope": scope})
            self._save(session)

            def progress(receipt):
                # Persist only the public receipt contract, never model tokens,
                # raw tool arguments, SQL, retrieved content, or credentials.
                item = {key: receipt[key] for key in ("id", "tool", "label", "status", "path", "detail")
                        if isinstance(receipt.get(key), str)}
                if not all(item.get(key) for key in ("id", "tool", "label")) or item.get("status") not in ("running", "complete", "error"):
                    return
                item = {key: value[:1000] for key, value in item.items()}

                def update(current):
                    activities = current.get("pending_activity", [])
                    for index, activity in enumerate(activities):
                        if activity["id"] == item["id"]:
                            activities[index] = item
                            break
                    else:
                        if len(activities) < MAX_ACTIVITIES:
                            activities.append(item)
                    current.update(pending_activity=activities, updated_at=timestamp())
                    return current

                current = self.store.config_update(self._key(chat_id), update)
                session["pending_activity"] = current["pending_activity"]

            def cancelled():
                return bool(self.store.config_get(self._key(chat_id), {}).get("cancel_requested"))

            try:
                reply = self.service.chat(message, mode, datasource_id, history, provider_id, repository_id,
                                          expected_vault=session["_vault_path"], expected_history_scope=scope,
                                          progress=progress, cancelled=cancelled)
                assistant = {"id": uuid.uuid4().hex, "role": "assistant", "content": reply["answer"], "_scope": scope}
                assistant.update({key: reply[key] for key in ("sources", "result", "provider", "usage", "activities", "completion") if key in reply})
                assistant.setdefault("activities", self._finished_activities(session["pending_activity"]))
            except Exception as exc:
                # Only known application errors are safe to display/persist.
                error = exc.message if isinstance(exc, ToolError) else "回复失败，请重试；原问题已保留。"
                assistant = {"id": uuid.uuid4().hex, "role": "assistant", "content": error, "error": error, "_scope": scope,
                             "activities": self._finished_activities(session["pending_activity"]), "completion": "limited"}
            session["messages"].append(assistant)
            session.update(pending=False, pending_activity=[], cancel_requested=False, updated_at=timestamp())
            self._save(session)
            return self._public(session)
