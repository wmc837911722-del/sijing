"""Provider adapters. Credentials never enter the application configuration database."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, quote

import httpx


class ToolError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_SECRET_MEMORY: dict[tuple[str, str], str] = {}
_SECRET_LOCK = threading.RLock()


class Secrets:
    """Only verified OS credential backends are eligible for persistent secrets."""
    def __init__(self, data_dir: Path):
        fingerprint = hashlib.sha256(str(Path(data_dir).resolve()).encode()).hexdigest()[:24]
        self.service = f"BusinessMap:{fingerprint}"
        self.backend = None
        try:
            import keyring
            backend = keyring.get_keyring()
            candidates = getattr(backend, "backends", [backend])
            for candidate in candidates:
                module = type(candidate).__module__
                if candidate.priority > 0 and any(module.startswith(prefix) for prefix in (
                    "keyring.backends.Windows", "keyring.backends.macOS",
                    "keyring.backends.SecretService", "keyring.backends.kwallet",
                )):
                    self.backend = candidate
                    break
        except Exception:
            # A headless Linux desktop may not have an unlocked credential service.
            pass

    def get(self, key: str) -> str | None:
        with _SECRET_LOCK:
            if (self.service, key) in _SECRET_MEMORY:
                return _SECRET_MEMORY[(self.service, key)]
        if self.backend:
            try:
                return self.backend.get_password(self.service, key)
            except Exception:
                pass
        return None

    def storage(self, key: str) -> str:
        with _SECRET_LOCK:
            if (self.service, key) in _SECRET_MEMORY:
                return "session"
        return "keyring" if self.get(key) else "none"

    def set(self, key: str, secret: str) -> str:
        if not isinstance(secret, str) or len(secret) > 16384:
            raise ToolError("凭据格式无效。")
        if not secret:
            with _SECRET_LOCK:
                _SECRET_MEMORY.pop((self.service, key), None)
            if self.backend:
                try:
                    self.backend.delete_password(self.service, key)
                except Exception:
                    if self.get(key):
                        raise ToolError("系统凭据库无法删除此凭据，请解锁后重试。")
            return "none"
        if self.backend:
            try:
                self.backend.set_password(self.service, key, secret)
                with _SECRET_LOCK:
                    _SECRET_MEMORY.pop((self.service, key), None)
                return "keyring"
            except Exception:
                pass
        with _SECRET_LOCK:
            _SECRET_MEMORY[(self.service, key)] = secret
        return "session"


def config_update(store, key, updater, default=None):
    if hasattr(store, "config_update"):
        return store.config_update(key, updater, default)
    value = updater(store.config_get(key, default))
    store.config_set(key, value)
    return value


def provider_destination(provider):
    """The exact model destination; labels and rotated credentials do not change consent."""
    return {key: provider[key] for key in ("id", "kind", "base_url", "model")}


def _aggregate_usage(previous, usage):
    previous = previous or {"requests": 0}
    count = previous["requests"]
    def total(field):
        current = usage.get(field)
        if count == 0:
            return current
        old = previous.get(field)
        return old + current if old is not None and current is not None else None
    return {"requests": count + 1, **{field: total(field) for field in ("input_tokens", "output_tokens", "cached_tokens")}}


class ProviderService:
    MAX_CONTEXT_CHARS = 48000
    DEFAULTS = {
        "openai-compatible": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com",
        "gemini": "https://generativelanguage.googleapis.com/v1beta",
        "codex-local": "codex://local",
    }

    def __init__(self, store):
        self.store = store
        self.secrets = Secrets(store.data_dir)

    def list(self):
        active = self.store.config_get("active_provider_id")
        output = []
        for item in self.store.config_get("providers", []):
            if item.get("kind") == "codex-local":
                from .codex_provider import status
                output.append({**{k: v for k, v in item.items() if k != "secret_ref"}, "has_key": False,
                               "secret_storage": "none", "active": item["id"] == active,
                               "capabilities": {"complete": True, "tool_turn": False}, "codex_status": status()})
                continue
            key = item.get("secret_ref", f"provider:{item['id']}")
            public = {k: v for k, v in item.items() if k != "secret_ref"}
            output.append({**public, "has_key": bool(self.secrets.get(key)),
                           "secret_storage": self.secrets.storage(key), "active": item["id"] == active})
        return output

    def get(self, provider_id=None):
        wanted = provider_id or self.store.config_get("active_provider_id")
        return next((p for p in self.list() if p["id"] == wanted), None)

    def save(self, data):
        kind = data.get("kind")
        if kind not in self.DEFAULTS:
            raise ToolError("不支持的模型供应商类型。")
        base = (data.get("base_url") or self.DEFAULTS[kind]).strip().rstrip("/")
        parts = urlsplit(base)
        if kind == "codex-local" and base != "codex://local":
            raise ToolError("Codex 仅支持本机登录连接。")
        if kind != "codex-local" and (parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment):
            raise ToolError("Base URL 必须是无内嵌凭据、查询参数和片段的 HTTP(S) 地址。")
        name, model = str(data.get("name", "")).strip(), str(data.get("model", "")).strip()
        if not name or not model or len(model) > 256 or len(name) > 160:
            raise ToolError("请填写供应商名称和模型名称。")
        provider_id = data.get("id") or uuid.uuid4().hex
        if data.get("id") and not self.get(provider_id):
            raise ToolError("模型配置不存在。", 404)
        item = {"id": provider_id, "name": name, "kind": kind, "base_url": base, "model": model}
        previous = next((p for p in self.store.config_get("providers", []) if p["id"] == provider_id), {})
        if kind == "codex-local" and data.get("api_key"):
            raise ToolError("本机 Codex 使用已有登录，不接受 API 密钥。")
        if kind != "codex-local":
            item["secret_ref"] = previous.get("secret_ref", f"provider:{provider_id}")
        if kind != "codex-local" and "api_key" in data and data["api_key"] is not None:
            # A failed persistent update must never resurrect an older key after restart.
            item["secret_ref"] = f"provider:{provider_id}:{uuid.uuid4().hex}"
            self.secrets.set(item["secret_ref"], data["api_key"])
        config_update(self.store, "providers", lambda old: [p for p in (old or []) if p["id"] != provider_id] + [item], [])
        if not self.store.config_get("active_provider_id"):
            self.store.config_set("active_provider_id", provider_id)
        return self.get(provider_id)

    def activate(self, provider_id):
        if not self.get(provider_id):
            raise ToolError("模型配置不存在。", 404)
        self.store.config_set("active_provider_id", provider_id)

    def usage(self):
        return self.store.config_get("provider_usage", {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": None})

    @classmethod
    def messages(cls, system, user, history=None, context=None):
        # Stable instructions always precede variable evidence and the current request.
        if len(system) + len(user) + len(context or "") > cls.MAX_CONTEXT_CHARS:
            raise ToolError("本次上下文超出 48,000 字符上限，请缩小问题或分析范围。")
        remaining = cls.MAX_CONTEXT_CHARS - len(system) - len(user) - len(context or "")
        tail = []
        for item in reversed((history or [])[-8:]):
            if item.get("role") not in ("user", "assistant"):
                continue
            content = str(item.get("content", ""))[:2000]
            if len(content) > remaining:
                break
            remaining -= len(content)
            tail.append({"role": item["role"], "content": content})
        prefix = [{"role": "system", "content": system}]
        if context:
            prefix.append({"role": "user", "content": context})
        return prefix + list(reversed(tail)) + [{"role": "user", "content": user}]

    @staticmethod
    def _tool_arguments(value):
        try:
            if isinstance(value, str):
                value = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict):
                raise ValueError()
            json.dumps(value, allow_nan=False)
            return value
        except (ValueError, TypeError):
            raise ToolError("模型工具参数必须是有效的 JSON 对象，未执行工具。", 502) from None

    @classmethod
    def _tool_transcript(cls, system, messages, tools):
        """Validate complete turns without silently dropping tool/result pairs."""
        try:
            size = len(json.dumps({"system": system, "messages": messages, "tools": tools},
                                  ensure_ascii=False, allow_nan=False))
        except (ValueError, TypeError):
            raise ToolError("工具对话格式无效。") from None
        if size > cls.MAX_CONTEXT_CHARS:
            raise ToolError("本次上下文超出 48,000 字符上限，请缩小问题或分析范围。")
        if not isinstance(system, str) or not isinstance(messages, list) or not messages or not isinstance(tools, list):
            raise ToolError("工具对话格式无效。")
        pending, seen = {}, set()
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in ("user", "assistant", "tool"):
                raise ToolError("工具对话消息格式无效。")
            role, content = message["role"], message.get("content")
            if not isinstance(content, str) and not (role == "assistant" and content is None):
                raise ToolError("工具对话内容格式无效。")
            if role == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    raise ToolError("工具结果缺少对应调用，未发送模型请求。")
                del pending[call_id]
                continue
            if pending:
                raise ToolError("工具调用尚未完成，未发送不完整对话。")
            if role != "assistant":
                if not content.strip():
                    raise ToolError("工具对话中不能包含空白用户消息。")
                continue
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list):
                raise ToolError("工具调用格式无效。")
            if not calls and not (content or "").strip():
                raise ToolError("工具对话中不能包含没有工具调用的空白助手消息。")
            for call in calls:
                call_id, name, _ = cls._canonical_call(call)
                if call_id in seen:
                    raise ToolError("工具调用标识重复，未发送模型请求。")
                seen.add(call_id)
                pending[call_id] = name
        if pending:
            raise ToolError("工具调用尚未完成，未发送不完整对话。")
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if (not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(function, dict)
                    or not isinstance(function.get("name"), str) or not function["name"]
                    or not isinstance(function.get("parameters"), dict)):
                raise ToolError("工具定义格式无效。")
        return [{"role": "system", "content": system}] + messages

    @classmethod
    def _canonical_call(cls, call):
        if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("function"), dict):
            raise ToolError("模型工具调用格式无效，未执行工具。", 502)
        call_id, name = call.get("id"), call["function"].get("name")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 256 or not isinstance(name, str) or not name or len(name) > 256:
            raise ToolError("模型工具调用缺少有效标识或名称，未执行工具。", 502)
        return call_id, name, cls._tool_arguments(call["function"].get("arguments"))

    @classmethod
    def _native_tool_messages(cls, kind, messages):
        output, call_names = [], {}
        for message in messages:
            role, content = message["role"], message.get("content") or ""
            calls = message.get("tool_calls") or []
            for call in calls:
                call_names[call["id"]] = call["function"]["name"]
            replay = message.get("_provider_replay", {})
            if kind == "openai-compatible":
                converted = {"role": role, "content": message.get("content")}
                if calls:
                    converted["tool_calls"] = calls
                if role == "tool":
                    converted["tool_call_id"] = message["tool_call_id"]
                elif role == "assistant" and replay.get("kind") == kind:
                    converted.update({key: value for key, value in replay.get("reasoning", {}).items()
                                      if key in ("reasoning_content", "reasoning_details")})
                output.append(converted)
                continue
            if role == "assistant" and replay.get("kind") == kind:
                parts = replay["content"]
            elif role == "tool":
                call_id = message["tool_call_id"]
                if kind == "anthropic":
                    parts = [{"type": "tool_result", "tool_use_id": call_id, "content": content}]
                else:
                    try:
                        result = json.loads(content)
                    except ValueError:
                        result = {"result": content}
                    if not isinstance(result, dict):
                        result = {"result": result}
                    response = {"name": call_names[call_id], "response": result}
                    # Gemini versions that emit call IDs expect them to be replayed.
                    if not call_id.startswith("gemini-local-"):
                        response["id"] = call_id
                    parts = [{"functionResponse": response}]
            else:
                parts = ([{"type": "text", "text": content}] if kind == "anthropic" else [{"text": content}]) if content else []
                for call in calls:
                    call_id, name, arguments = cls._canonical_call(call)
                    if kind == "anthropic":
                        parts.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
                    else:
                        parts.append({"functionCall": {"name": name, "args": arguments}})
            native_role = ("assistant" if kind == "anthropic" else "model") if role == "assistant" else "user"
            field = "content" if kind == "anthropic" else "parts"
            # Parallel tool results must be adjacent within one native user turn.
            if output and output[-1]["role"] == native_role:
                output[-1][field] = output[-1][field] + parts
            else:
                output.append({"role": native_role, field: parts})
        return output

    @classmethod
    def _tool_response(cls, kind, data):
        replay, calls = None, []
        if kind == "openai-compatible":
            choice = data["choices"][0]
            native = choice["message"]
            text = native.get("content") or ""
            calls = native.get("tool_calls") or []
            reasoning = {key: native[key] for key in ("reasoning_content", "reasoning_details") if key in native}
            if reasoning:
                replay = {"kind": kind, "reasoning": reasoning}
            if choice.get("finish_reason") == "length" and calls:
                raise ToolError("模型工具调用因输出额度不足而中断，未执行工具。", 502)
        else:
            native = data.get("content", []) if kind == "anthropic" else data["candidates"][0]["content"]["parts"]
            replay = {"kind": kind, "content": native}
            text = "\n".join(part.get("text", "") for part in native
                             if (part.get("type") == "text" if kind == "anthropic" else not part.get("thought")))
            for part in native:
                if kind == "anthropic" and part.get("type") == "tool_use":
                    call_id, name, arguments = part.get("id"), part.get("name"), part.get("input")
                elif kind == "gemini" and "functionCall" in part:
                    function = part["functionCall"]
                    call_id = function.get("id") or "gemini-local-" + uuid.uuid4().hex
                    name, arguments = function.get("name"), function.get("args", {})
                else:
                    continue
                calls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}})
            stopped = (data.get("stop_reason") == "max_tokens" if kind == "anthropic"
                       else data["candidates"][0].get("finishReason") == "MAX_TOKENS")
            if stopped and calls:
                raise ToolError("模型工具调用因输出额度不足而中断，未执行工具。", 502)
        if not isinstance(text, str) or not isinstance(calls, list):
            raise ToolError("模型工具响应格式无效。", 502)
        normalized, canonical, seen = [], [], set()
        for call in calls:
            call_id, name, arguments = cls._canonical_call(call)
            if call_id in seen:
                raise ToolError("模型工具调用标识重复，未执行工具。", 502)
            seen.add(call_id)
            normalized.append({"id": call_id, "name": name, "arguments": arguments})
            canonical.append({"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))}})
        assistant = {"role": "assistant", "content": text}
        if canonical:
            assistant["tool_calls"] = canonical
        if replay:
            # Internal transport state only. Never persist this in user-visible history.
            assistant["_provider_replay"] = replay
        return text, normalized, assistant

    def tool_turn(self, system, messages, tools, provider_id=None, max_output=2200,
                  expected_destination=None, before_send=None, timeout=60):
        """One model-controlled step; the caller owns execution and loop budgets."""
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise ToolError("工具对话格式无效。")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ToolError("模型调用已达到时间限制。", 408)
        return self.complete(system, "", provider_id=provider_id, max_output=max_output,
                             expected_destination=expected_destination, before_send=before_send,
                             _tool_messages=messages, _tools=tools, _deadline=time.monotonic() + timeout)

    def complete(self, system, user, history=None, provider_id=None, max_output=2200, context=None,
                 expected_destination=None, before_send=None, _connection_test=False,
                 _tool_messages=None, _tools=None, _deadline=None, json_mode=False):
        provider = self.get(provider_id)
        if not provider:
            raise ToolError("尚未配置并启用模型。", 409)
        if expected_destination is not None and provider_destination(provider) != expected_destination:
            raise ToolError("模型目标已变化，本次请求未发送。请确认模型配置并重新绑定仓库。", 409)
        if provider["kind"] == "codex-local":
            if _tool_messages is not None:
                raise ToolError("本机 Codex 当前仅支持代码分析和业务归纳，暂不支持工具问答。", 409)
            from .codex_provider import complete
            messages = self.messages(system, user, history, context)
            destination = provider_destination(provider)
            def guard():
                latest = next((p for p in self.store.config_get("providers", []) if p["id"] == provider["id"]), None)
                if latest is None or provider_destination(latest) != destination:
                    raise ToolError("模型目标已变化，本次请求已停止。", 409)
                if before_send is not None:
                    before_send()
            result = complete(messages, provider["model"], max_output=max_output, before_send=guard,
                              timeout=None if _deadline is None else _deadline - time.monotonic())
            usage = result["usage"]
            config_update(self.store, "provider_usage", lambda old: _aggregate_usage(old, usage))
            return {**result, "provider": provider["name"]}
        tools_enabled = _tool_messages is not None
        messages = self._tool_transcript(system, _tool_messages, _tools) if tools_enabled else self.messages(system, user, history, context)
        metadata = next((p for p in self.store.config_get("providers", []) if p["id"] == provider["id"]), None)
        # Read credentials from the same destination revision. A concurrent edit
        # must never combine the previous endpoint with the new endpoint's key.
        if metadata is None or provider_destination(metadata) != provider_destination(provider):
            raise ToolError("模型目标已变化，本次请求未发送。请确认模型配置并重新绑定仓库。", 409)
        key = self.secrets.get(metadata.get("secret_ref", f"provider:{provider['id']}"))
        kind, base, model = provider["kind"], provider["base_url"], provider["model"]
        destination = urlsplit(base)
        if key and destination.scheme != "https" and not is_loopback(destination.hostname):
            raise ToolError("带 API Key 的远端模型请求必须使用 HTTPS；HTTP 仅允许本机回环地址。", 403)
        headers = {"Content-Type": "application/json"}
        if kind == "openai-compatible":
            url = base if base.endswith("/chat/completions") else base + "/chat/completions"
            if key:
                headers["Authorization"] = f"Bearer {key}"
            token_parameter = "max_completion_tokens" if destination.hostname == "api.openai.com" and (model.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6"))) else "max_tokens"
            payload = {"model": model, "messages": messages, token_parameter: max_output}
            if tools_enabled:
                payload["messages"] = [messages[0]] + self._native_tool_messages(kind, messages[1:])
                if _tools:
                    payload["tools"] = _tools
            if destination.hostname in ("api.minimax.cn", "api.minimax.io", "api.minimaxi.com"):
                # Keep reasoning out of business answers and JSON/SQL responses.
                payload["reasoning_split"] = True
            if destination.hostname == "api.deepseek.com":
                # Reserve the bounded output budget for answers and tool calls.
                payload["thinking"] = {"type": "disabled"}
                if json_mode:
                    # Opt in only for the official endpoint. Compatible
                    # gateways and ordinary chat may not support JSON mode.
                    payload["response_format"] = {"type": "json_object"}
            if (destination.hostname == "spark-api-open.xf-yun.com"
                    and destination.path.removesuffix("/chat/completions") in ("/v1", "/v2", "/x2")):
                # Spark v1 otherwise enables external web search by default.
                payload["tools"] = payload.get("tools", []) + [{"type": "web_search", "web_search": {"enable": False}}]
        elif kind == "anthropic":
            if not key:
                raise ToolError("Anthropic API Key 未配置或当前凭据不可用。", 409)
            url = base if base.endswith("/messages") else base + ("/messages" if base.endswith("/v1") else "/v1/messages")
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            payload = {"model": model, "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}], "messages": messages[1:], "max_tokens": max_output}
            if context:
                payload["messages"][0] = {"role": "user", "content": [{"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}]}
            if tools_enabled:
                payload["messages"] = self._native_tool_messages(kind, messages[1:])
                last_message = payload["messages"][-1]
                if last_message["role"] == "user" and last_message["content"]:
                    # Keep the growing evidence prefix cacheable as well as the
                    # fixed tools/system prefix. Never annotate signed thinking.
                    last_message["content"][-1] = {**last_message["content"][-1],
                                                  "cache_control": {"type": "ephemeral"}}
                if _tools:
                    payload["tools"] = [{"name": tool["function"]["name"],
                                         "description": tool["function"].get("description", ""),
                                         "input_schema": tool["function"]["parameters"]} for tool in _tools]
        else:
            if not key:
                raise ToolError("Gemini API Key 未配置或当前凭据不可用。", 409)
            url = base + "/models/" + quote(model.removeprefix("models/"), safe="") + ":generateContent"
            headers["x-goog-api-key"] = key
            payload = {"systemInstruction": {"parts": [{"text": system}]},
                       "contents": self._native_tool_messages(kind, messages[1:]) if tools_enabled else [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]} for m in messages[1:]],
                       "generationConfig": {"maxOutputTokens": max_output}}
            if tools_enabled and _tools:
                # `parameters` is Gemini's OpenAPI subset (no additionalProperties,
                # int64 length fields). Our tools are full JSON Schema objects.
                payload["tools"] = [{"functionDeclarations": [{"name": tool["function"]["name"],
                                                               "description": tool["function"].get("description", ""),
                                                               "parametersJsonSchema": tool["function"]["parameters"]} for tool in _tools]}]
        truncated_reasoning = False
        tool_calls, assistant_message = [], None
        try:
            if before_send is not None:
                before_send()
            remaining = 120 if _deadline is None else _deadline - time.monotonic()
            if remaining <= 0:
                raise ToolError("模型调用已达到时间限制。", 408)
            with httpx.Client(timeout=httpx.Timeout(remaining, connect=min(10, remaining)), follow_redirects=False) as client:
                latest = next((p for p in self.store.config_get("providers", []) if p["id"] == provider["id"]), None)
                if latest is None or provider_destination(latest) != provider_destination(provider):
                    raise ToolError("模型目标已变化，本次请求未发送。请确认模型配置并重新绑定仓库。", 409)
                response = client.post(url, headers=headers, json=payload)
            if _deadline is not None and time.monotonic() >= _deadline:
                raise ToolError("模型调用已达到时间限制，未执行本轮工具。", 408)
            if response.status_code >= 300:
                raise ToolError(f"模型服务返回 HTTP {response.status_code}。请检查模型、地址、凭据或额度。", 502)
            data = response.json()
            if kind == "openai-compatible":
                choice = data["choices"][0]
                answer = choice["message"].get("content")
                reasoning = choice["message"].get("reasoning_content")
                truncated_reasoning = (choice.get("finish_reason") == "length"
                                       and (answer is None or isinstance(answer, str) and not answer.strip())
                                       and isinstance(reasoning, str) and bool(reasoning.strip()))
                raw = data.get("usage") or {}
                usage = {"input_tokens": raw.get("prompt_tokens"), "output_tokens": raw.get("completion_tokens"),
                         "cached_tokens": (raw.get("prompt_tokens_details") or {}).get("cached_tokens", raw.get("prompt_cache_hit_tokens")),
                         "cache_miss_tokens": raw.get("prompt_cache_miss_tokens")}
            elif kind == "anthropic":
                answer = "\n".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text")
                raw = data.get("usage") or {}
                total_in = raw.get("input_tokens")
                if total_in is not None:
                    total_in += (raw.get("cache_read_input_tokens") or 0) + (raw.get("cache_creation_input_tokens") or 0)
                usage = {"input_tokens": total_in, "output_tokens": raw.get("output_tokens"), "cached_tokens": raw.get("cache_read_input_tokens"),
                         "cache_creation_input_tokens": raw.get("cache_creation_input_tokens"), "cache_read_input_tokens": raw.get("cache_read_input_tokens")}
            else:
                answer = "\n".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"] if not p.get("thought"))
                raw = data.get("usageMetadata") or {}
                usage = {"input_tokens": raw.get("promptTokenCount"), "output_tokens": raw.get("candidatesTokenCount"), "cached_tokens": raw.get("cachedContentTokenCount")}
            if tools_enabled:
                answer, tool_calls, assistant_message = self._tool_response(kind, data)
            if truncated_reasoning:
                # A tiny connection probe may end during reasoning. Never expose
                # that reasoning as an answer or accept it for normal requests.
                answer = ""
            elif (not isinstance(answer, str) or not answer.strip()) and not tool_calls:
                raise ToolError("模型未返回可用文本，未执行任何查询或写入。", 502)
        except ToolError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            # Never include HTTP bodies, headers or exception URLs in errors/logs.
            raise ToolError(f"模型调用失败（{type(exc).__name__}），未获得有效结果。", 502) from None
        usage = {k: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None for k, v in usage.items()}
        config_update(self.store, "provider_usage", lambda old: _aggregate_usage(old, usage))
        if truncated_reasoning and not _connection_test:
            raise ToolError("模型已耗尽输出额度，思考后仍未返回正文。请尝试非思考模型或其他模型；未执行任何查询或写入。", 502)
        output_limited = (data["choices"][0].get("finish_reason") == "length" if kind == "openai-compatible"
                          else data.get("stop_reason") == "max_tokens" if kind == "anthropic"
                          else data["candidates"][0].get("finishReason") == "MAX_TOKENS")
        result = {"text": answer.strip(), "usage": usage, "provider": provider["name"], "output_limited": output_limited}
        if tools_enabled:
            result.update({"tool_calls": tool_calls, "assistant_message": assistant_message})
        if _connection_test:
            result["output_limited"] = truncated_reasoning
        return result


def parse_json_answer(text):
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1])
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        raise ToolError("模型输出不是所需 JSON 格式，未执行查询或写入。", 502) from None
