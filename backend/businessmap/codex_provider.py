"""Bounded text-only Codex subprocesses; credentials remain owned by Codex."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from .providers import ToolError
from . import _codex_dispatch

TIMEOUT_SECONDS = 180
STRUCTURED_TIMEOUT_SECONDS = 360
MAX_EVENT_BYTES = 512_000
MAX_INPUT_BYTES = 192_000
_STATUS_CACHE = {}
_STATUS_LOCK = threading.Lock()
_CATALOG_CACHE = {}
_CATALOG_LOCK = threading.Lock()
_SAFE_STARTUP_DIAGNOSTIC = "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`."
CODEX_FAILURE_ACTIONS = {
    "quota": "Codex 账号额度或速率受限，请稍后继续；成功断点已保留。",
    "login": "Codex 登录不可用，请在本机重新使用 ChatGPT 登录后继续；成功断点已保留。",
    "timeout": "Codex 分析进程超时，请稍后继续未完成批次；成功断点已保留。",
    "config": "Codex 版本、模型或隔离配置未通过核验，请检查本机 Codex 后继续；成功断点已保留。",
    "invalid_event": "Codex 返回的事件格式无效，请检查 CLI 版本后继续；成功断点已保留。",
    "invalid_json": "Codex 未返回有效 JSON 正文，请继续未完成批次；成功断点已保留。",
    "process_exit": "Codex 分析进程异常退出，请检查本机 Codex 后继续；成功断点已保留。",
    "incomplete": "Codex 响应未正常完成，请继续未完成批次；成功断点已保留。",
    "communication": "Codex 进程通信失败，请检查本机运行环境后继续；成功断点已保留。",
    "unexpected_tool": "Codex 请求了未获授权的工具，进程已终止；请检查隔离配置，成功断点已保留。",
    "input_limit": "Codex 分析输入超过大小上限，请调整分片后继续；成功断点已保留。",
    "output_limit": "Codex 返回结果超过大小上限，请检查输出预算后继续；成功断点已保留。",
    "empty_response": "Codex 未返回有效正文，请继续未完成批次；成功断点已保留。",
    "process_cleanup": "Codex 进程组未能完整终止，请检查本机进程后继续；成功断点已保留。",
    "stream_error": "Codex 模型响应流中断，请稍后继续未完成批次；成功断点已保留。",
    "provider_error": "Codex 模型服务返回调用错误，请检查服务状态后继续；成功断点已保留。",
}


def _codex_error(reason, message, status_code=502):
    error = ToolError(message, status_code)
    error.codex_reason = reason if reason in CODEX_FAILURE_ACTIONS else "provider_error"
    return error

# Do not inherit the desktop's tools, user configuration, hooks, or source tree.
# Unknown configuration/flags must fail before a request, never silently fall back.
DISABLED_FEATURES = ("shell_tool", "shell_snapshot", "hooks", "plugins", "remote_plugin", "apps",
                     "multi_agent", "multi_agent_v2", "browser_use", "browser_use_external", "computer_use",
                     "image_generation", "view_image", "sleep_tool", "skill_search", "skill_mcp_dependency_install",
                     "memories", "chronicle", "goals", "code_mode", "tool_suggest", "artifact",
                     "workspace_dependencies", "unbounded_connection_retries", "default_mode_request_user_input")
_ENV_NAMES = {"HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SystemRoot", "SYSTEMROOT", "WINDIR",
              "PATH", "PATHEXT", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"}


def find_codex():
    executable = shutil.which("codex")
    if executable:
        return str(Path(executable).resolve())
    for candidate in ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
                      "/Applications/Codex.app/Contents/Resources/codex"):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _environment():
    # In particular do not pass API keys, access tokens, CODEX_HOME, thread IDs,
    # configuration overrides, shell startup variables, or application secrets.
    return {key: value for key, value in os.environ.items() if key in _ENV_NAMES}


def _text_only_catalog(executable, version):
    """Pin tool routing independently of a model's bundled Direct preference."""
    key = executable, version
    with _CATALOG_LOCK:
        if key in _CATALOG_CACHE:
            return _CATALOG_CACHE[key]
        try:
            with tempfile.TemporaryDirectory(prefix="sijing-codex-models-") as directory:
                result = subprocess.run([executable, "debug", "models", "--bundled"], cwd=directory,
                                        env=_environment(), stdin=subprocess.DEVNULL, capture_output=True,
                                        timeout=10, check=False)
            if result.returncode != 0 or len(result.stdout) > 2_000_000:
                raise ValueError()
            catalog = json.loads(result.stdout)
            models = catalog["models"]
            if not isinstance(models, list) or not models or len(models) > 200:
                raise ValueError()
            for model in models:
                if not isinstance(model, dict) or not isinstance(model.get("slug"), str):
                    raise ValueError()
                model["tool_mode"] = "code_mode_only"
                model["apply_patch_tool_type"] = None
            value = json.dumps(catalog, ensure_ascii=False)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            raise _codex_error("config", "无法核验 Codex 内置模型工具边界，未发送分析请求。", 409) from None
        _CATALOG_CACHE[key] = value
        return value


def status():
    executable = find_codex()
    if not executable:
        return {"installed": False, "logged_in": None, "status": "missing", "version": None,
                "detail": "未找到本机 Codex CLI，请先安装并使用 ChatGPT 登录。"}
    if os.name != "posix":
        return {"installed": True, "logged_in": None, "status": "unsupported", "version": None,
                "detail": "当前系统尚未提供可核验的 Codex 进程组隔离；请使用其他模型服务。"}
    with _STATUS_LOCK:
        cached = _STATUS_CACHE.get(executable)
        if cached and time.monotonic() - cached[0] < 30:
            return dict(cached[1])
        value = {"installed": True, "logged_in": None, "status": "error", "version": None,
                 "detail": "无法核验本机 Codex，请检查安装与 ChatGPT 登录。"}
        try:
            with tempfile.TemporaryDirectory(prefix="sijing-codex-check-") as directory:
                version = subprocess.run([executable, "--version"], cwd=directory, env=_environment(),
                                         stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False)
                match = re.fullmatch(rb"codex-cli ([0-9][A-Za-z0-9.+-]{0,80})\s*", version.stdout)
                if version.returncode != 0 or not match:
                    return value
                value["version"] = match[1].decode("ascii")
                numbers = tuple(int(part) for part in value["version"].split("-")[0].split(".")[:3])
                if numbers < (0, 163, 0):
                    value.update(status="unsupported", detail="本机 Codex 版本不支持所需隔离参数，请先更新 Codex。")
                else:
                    login = subprocess.run([executable, "login", "status"], cwd=directory, env=_environment(),
                                           stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False)
                    # Inspect only the known status marker, never expose login output.
                    logged_in = login.returncode == 0 and b"logged in using chatgpt" in (login.stdout + login.stderr).lower()
                    value.update(logged_in=logged_in, status="ready" if logged_in else "login_required",
                                 detail="已检测到本机 ChatGPT 登录；仅支持代码分析和业务归纳。" if logged_in else
                                 "请先在本机 Codex 使用 ChatGPT 登录；不使用 API Key 登录。")
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        _STATUS_CACHE[executable] = time.monotonic(), value
        return dict(value)


def _stop(process):
    try:
        if os.name == "posix":
            # The leader may already have exited while a descendant survives.
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can deny a group signal while its leader is exiting but has
        # not yet been reaped. Reap that race before deciding cleanup failed.
        try:
            process.wait(timeout=.15)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except OSError:
                raise _codex_error("process_cleanup", "Codex 进程组无法完整终止，未接受本次结果。") from None
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                raise _codex_error("process_cleanup", "Codex 进程组无法完整终止，未接受本次结果。") from None
        # Reaping/killing the leader alone is insufficient: descendants may
        # still own the original group and must also be terminated.
        try:
            if os.name != "posix":
                raise PermissionError()
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            raise _codex_error("process_cleanup", "Codex 进程组无法完整终止，未接受本次结果。") from None
    process.wait()


def _failure(event):
    error = event.get("error")
    message = str(error.get("message", "") if isinstance(error, dict) else event.get("message", "")).lower()
    if any(term in message for term in ("usage limit", "rate limit", "quota", "credits", "429")):
        return _codex_error("quota", "Codex 账号额度或速率受限，成功断点保留，请稍后继续。", 429)
    if any(term in message for term in ("unauthorized", "authentication", "sign in", "401")):
        return _codex_error("login", "Codex 登录已失效，请在本机重新使用 ChatGPT 登录。", 401)
    if any(term in message for term in ("unknown configuration", "invalid schema", "unsupported configuration")):
        return _codex_error("config", "Codex 模型或隔离配置未通过服务核验，未接受结果。")
    if any(term in message for term in ("stream disconnected", "stream closed", "stream error", "stream interrupted")):
        return _codex_error("stream_error", "Codex 模型响应流中断，未接受不完整结果。")
    return _codex_error("provider_error", "本机 Codex 调用失败，未接受不完整结果。")


def _request_payload(messages, max_output, output_schema=None):
    budget = min(128_000, max(1024, max_output * 16))
    instructions = "只分析提供的消息资料，不执行命令或工具，不读取本地文件。按消息要求回答；把最终正文完整放入answer字符串。如果要求JSON，answer内容必须是该JSON原文。正文尽量不超过指定token预算。"
    schema = {"type": "object", "properties": {"answer": {"type": "string", "maxLength": budget}},
              "required": ["answer"], "additionalProperties": False}
    if output_schema is not None:
        if not isinstance(output_schema, dict) or output_schema.get("type") != "object":
            raise _codex_error("config", "Codex 结构化输出配置无效。", 400)
        instructions = "只分析提供的消息资料，不执行命令或工具，不读取本地文件。按消息要求回答；把完整JSON对象放入answer对象，严格遵守输出结构。正文尽量不超过指定token预算。"
        answer_schema = deepcopy(output_schema)
        if "$defs" in answer_schema:
            schema["$defs"] = answer_schema.pop("$defs")
        schema["properties"]["answer"] = answer_schema
    try:
        prompt = json.dumps({"instructions": instructions, "max_output_tokens": max_output, "messages": messages}, ensure_ascii=False)
        schema_wire = json.dumps(schema, allow_nan=False)
    except (TypeError, ValueError):
        raise _codex_error("config", "Codex 结构化输出配置无效。", 400) from None
    return prompt, schema_wire, budget


def request_input_bytes(messages, *, max_output, output_schema):
    """Exact serialized structured prompt plus the schema sent to Codex."""
    prompt, schema_wire, _ = _request_payload(messages, max_output, output_schema)
    return len(prompt.encode()) + len(schema_wire.encode())


def complete(messages, model, *, max_output, before_send, timeout=None, output_schema=None, absolute_deadline=None,
             execution_timeout=None):
    with _codex_dispatch.native_attempt() as dispatch:
        return _complete(messages, model, max_output=max_output, before_send=before_send, timeout=timeout,
                         output_schema=output_schema, absolute_deadline=absolute_deadline,
                         execution_timeout=execution_timeout, dispatch=dispatch)


def _complete(messages, model, *, max_output, before_send, timeout, output_schema, absolute_deadline,
              execution_timeout, dispatch):
    if not isinstance(max_output, int) or isinstance(max_output, bool) or not 1 <= max_output <= 16000:
        raise _codex_error("config", "Codex 输出额度无效。", 400)
    for value in (timeout, absolute_deadline, execution_timeout):
        if value is not None and (type(value) not in (int, float) or value <= 0
                                  or not math.isfinite(value)):
            raise _codex_error("timeout", "Codex 调用已达到时间限制。", 408)
    entry = time.monotonic()
    ceiling = STRUCTURED_TIMEOUT_SECONDS if output_schema is not None else TIMEOUT_SECONDS
    duration = TIMEOUT_SECONDS if timeout is None and absolute_deadline is None else ceiling
    if timeout is not None:
        duration = min(duration, timeout)
    deadline = entry + duration
    if absolute_deadline is not None:
        deadline = min(deadline, absolute_deadline)
    state = status()
    if state["status"] != "ready":
        raise _codex_error("login" if state["status"] == "login_required" else "config", state["detail"], 409)
    executable = find_codex()
    prompt, schema_wire, budget = _request_payload(messages, max_output, output_schema)
    input_bytes = len(prompt.encode()) + (len(schema_wire.encode()) if output_schema is not None else 0)
    if input_bytes > MAX_INPUT_BYTES:
        raise _codex_error("input_limit", "Codex 输入超过大小上限，未发送。", 422)
    with tempfile.TemporaryDirectory(prefix="sijing-codex-") as directory:
        root = Path(directory)
        schema_path = root / "answer.schema.json"
        schema_path.write_text(schema_wire, encoding="utf-8")
        catalog_path = root / "text-only-models.json"
        catalog_path.write_text(_text_only_catalog(executable, state["version"]), encoding="utf-8")
        # Source content stays on stdin, out of process argv and config overrides.
        arguments = [executable, "--no-daemon", "-a", "never", "exec", "--ignore-user-config", "--ignore-rules",
                     "--strict-config", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "--json",
                     "--color", "never", "--output-schema", str(schema_path)]
        overrides = [f"features.{name}=false" for name in DISABLED_FEATURES] + [
            "features.skip_host_skill_discovery=true", 'web_search="disabled"',
            "features.code_mode_host={enabled=false,disable_in_process_fallback=true}", "features.code_mode_only=true",
            "model_catalog_json=" + json.dumps(str(catalog_path)),
            "project_doc_max_bytes=0", "mcp_servers={}", "hooks={}", 'shell_environment_policy.inherit="none"',
            'model_reasoning_effort="low"', "suppress_unstable_features_warning=true"]
        for override in overrides:
            arguments.extend(["-c", override])
        if model != "default":
            arguments.extend(["--model", model])
        arguments.append("-")
        # Independent read/write handles avoid shared seek offsets on Windows.
        with (root / "events.jsonl").open("w+b") as output, (root / "events.jsonl").open("rb") as reader, tempfile.TemporaryFile() as prompt_input:
            prompt_input.write(prompt.encode())
            prompt_input.seek(0)
            dispatch.check_control(before_send)
            ready = time.monotonic()
            if execution_timeout is not None:
                # Only the semantic execution window starts after local admission.
                # An explicit caller deadline or timeout still includes preparation.
                process_deadline = ready + min(execution_timeout, ceiling)
                deadline = process_deadline if timeout is None and absolute_deadline is None else min(deadline, process_deadline)
            if ready >= deadline:
                raise _codex_error("timeout", "Codex 调用已达到时间限制。", 408)
            environment = _environment()
            # Once process creation is attempted, zero dispatch is no longer proven.
            dispatch.launch_attempted = True
            try:
                process = subprocess.Popen(arguments, cwd=directory, env=environment, shell=False,
                                           stdin=prompt_input, stdout=output, stderr=subprocess.DEVNULL,
                                           start_new_session=os.name == "posix")
            except OSError:
                raise _codex_error("config", "无法启动本机 Codex，请检查安装。") from None
            checked_at = time.monotonic()
            offset, pending, final, usage, completed = 0, b"", None, {}, False
            try:
                while True:
                    if time.monotonic() >= deadline:
                        raise _codex_error("timeout", "Codex 调用超时，已终止本次分析进程，成功断点保留。", 408)
                    size = os.fstat(output.fileno()).st_size
                    if size > MAX_EVENT_BYTES:
                        raise _codex_error("output_limit", "Codex 返回结果超过大小上限，未接受结果。")
                    if size > offset:
                        reader.seek(offset)
                        data = reader.read(size - offset)
                        offset = size
                        pending += data
                        lines = pending.split(b"\n")
                        pending = lines.pop()
                        for line in lines:
                            if completed:
                                raise _codex_error("invalid_event", "Codex 完成后返回额外事件，未接受结果。")
                            try:
                                event = json.loads(line)
                            except (ValueError, UnicodeError):
                                raise _codex_error("invalid_event", "Codex 返回结果格式无效，未接受结果。") from None
                            if not isinstance(event, dict):
                                raise _codex_error("invalid_event", "Codex 返回结果格式无效，未接受结果。")
                            kind = event.get("type")
                            if kind in {"error", "turn.failed"}:
                                raise _failure(event)
                            if kind in {"item.started", "item.updated", "item.completed"}:
                                item = event.get("item") or {}
                                if not isinstance(item, dict):
                                    raise _codex_error("invalid_event", "Codex 返回结果格式无效，未接受结果。")
                                if item.get("type") == "error":
                                    if kind == "item.completed" and item.get("message") == _SAFE_STARTUP_DIAGNOSTIC:
                                        continue
                                    raise _failure({"error": item})
                                if item.get("type") not in {"agent_message", "reasoning"}:
                                    raise _codex_error("unexpected_tool", "Codex 请求了未授权工具，已终止进程，未接受结果。", 403)
                                if kind == "item.completed" and item.get("type") == "agent_message":
                                    final = item.get("text")
                            elif kind == "turn.completed":
                                completed, usage = True, event.get("usage") or {}
                                if not isinstance(usage, dict):
                                    raise _codex_error("invalid_event", "Codex 返回结果格式无效，未接受结果。")
                            elif kind not in {"thread.started", "turn.started"}:
                                raise _codex_error("invalid_event", "Codex 返回未支持的事件，未接受结果。")
                    # The full event stream, answer and clean process exit must all
                    # be observed before the fixed deadline. A slow guard cannot
                    # turn an already observed completion into a late result.
                    if time.monotonic() >= deadline:
                        raise _codex_error("timeout", "Codex 调用超时，已终止本次分析进程，成功断点保留。", 408)
                    exited = process.poll() is not None
                    candidate = None
                    if exited and os.fstat(output.fileno()).st_size == offset:
                        if process.returncode != 0:
                            raise _codex_error("process_exit", "本机 Codex 调用失败，请检查登录、版本或额度；成功断点保留。")
                        if not completed or pending or not isinstance(final, str):
                            raise _codex_error("incomplete", "Codex 调用未正常完成，未接受结果。")
                        try:
                            answer = json.loads(final)["answer"]
                            if output_schema is not None:
                                if not isinstance(answer, dict):
                                    raise ValueError()
                                answer = json.dumps(answer, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                        except (KeyError, TypeError, ValueError):
                            raise _codex_error("invalid_json", "Codex 返回结果格式无效，未接受结果。") from None
                        if not isinstance(answer, str) or not answer.strip():
                            raise _codex_error("empty_response", "Codex 未返回有效结果。")
                        safe_usage = {target: value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
                                      for target, value in (("input_tokens", usage.get("input_tokens")), ("output_tokens", usage.get("output_tokens")),
                                                            ("cached_tokens", usage.get("cached_input_tokens")))}
                        limited = len(answer.encode()) > budget or (safe_usage["output_tokens"] or 0) >= max_output
                        candidate = {"text": answer.strip(), "usage": safe_usage, "output_limited": limited,
                                     "runtime": {"kind": "codex-local", "version": state["version"], "model": model}}
                        if time.monotonic() >= deadline:
                            raise _codex_error("timeout", "Codex 调用超时，已终止本次分析进程，成功断点保留。", 408)
                    if time.monotonic() - checked_at >= .5:
                        dispatch.check_control(before_send)
                        checked_at = time.monotonic()
                    if candidate is not None:
                        dispatch.check_control(before_send)
                        # Never add bytes observed after capture; even a trailing
                        # partial event invalidates the captured prefix.
                        if os.fstat(output.fileno()).st_size != offset:
                            raise _codex_error("incomplete", "Codex 调用未正常完成，未接受结果。")
                        return candidate
                    if exited:
                        continue
                    time.sleep(.05)
            except (OSError, UnicodeError) as error:
                if dispatch.control_error is error:
                    raise
                raise _codex_error("communication", "本机 Codex 进程通信失败，未接受结果。") from None
            finally:
                _stop(process)
