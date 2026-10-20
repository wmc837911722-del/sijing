"""Bounded text-only Codex subprocesses; credentials remain owned by Codex."""
from __future__ import annotations

import json
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

TIMEOUT_SECONDS = 180
MAX_EVENT_BYTES = 512_000
MAX_INPUT_BYTES = 192_000
_STATUS_CACHE = {}
_STATUS_LOCK = threading.Lock()
_CATALOG_CACHE = {}
_CATALOG_LOCK = threading.Lock()
_SAFE_STARTUP_DIAGNOSTIC = "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`."

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
            raise ToolError("无法核验 Codex 内置模型工具边界，未发送分析请求。", 409) from None
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
        # macOS may return EPERM for a group containing only an exited leader.
        # A live owned process must still be stopped rather than ignored.
        if process.poll() is None:
            process.kill()
            process.wait()
            raise ToolError("Codex 进程组无法完整终止，未接受本次结果。", 502) from None
    process.wait()


def _failure(event):
    error = event.get("error")
    message = str(error.get("message", "") if isinstance(error, dict) else event.get("message", "")).lower()
    if any(term in message for term in ("usage limit", "rate limit", "quota", "credits", "429")):
        return ToolError("Codex 账号额度或速率受限，成功断点保留，请稍后继续。", 429)
    if any(term in message for term in ("unauthorized", "authentication", "sign in", "401")):
        return ToolError("Codex 登录已失效，请在本机重新使用 ChatGPT 登录。", 401)
    return ToolError("本机 Codex 调用失败，未接受不完整结果。", 502)


def complete(messages, model, *, max_output, before_send, timeout=None):
    if not isinstance(max_output, int) or isinstance(max_output, bool) or not 1 <= max_output <= 16000:
        raise ToolError("Codex 输出额度无效。")
    timeout = TIMEOUT_SECONDS if timeout is None else min(TIMEOUT_SECONDS, timeout)
    if timeout <= 0:
        raise ToolError("Codex 调用已达到时间限制。", 408)
    state = status()
    if state["status"] != "ready":
        raise ToolError(state["detail"], 409)
    executable = find_codex()
    budget = min(128_000, max(1024, max_output * 16))
    prompt = json.dumps({"instructions": "只分析提供的消息资料，不执行命令或工具，不读取本地文件。按消息要求回答；把最终正文完整放入answer字符串。如果要求JSON，answer内容必须是该JSON原文。正文尽量不超过指定token预算。",
                         "max_output_tokens": max_output, "messages": messages}, ensure_ascii=False)
    if len(prompt.encode()) > MAX_INPUT_BYTES:
        raise ToolError("Codex 输入超过大小上限，未发送。", 422)
    schema = {"type": "object", "properties": {"answer": {"type": "string", "maxLength": budget}},
              "required": ["answer"], "additionalProperties": False}
    with tempfile.TemporaryDirectory(prefix="sijing-codex-") as directory:
        root = Path(directory)
        schema_path = root / "answer.schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
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
            before_send()
            try:
                process = subprocess.Popen(arguments, cwd=directory, env=_environment(), shell=False,
                                           stdin=prompt_input, stdout=output, stderr=subprocess.DEVNULL,
                                           start_new_session=os.name == "posix")
            except OSError:
                raise ToolError("无法启动本机 Codex，请检查安装。", 502) from None
            started = checked_at = time.monotonic()
            offset, pending, final, usage, completed = 0, b"", None, {}, False
            try:
                while True:
                    if time.monotonic() - checked_at >= .5:
                        before_send()
                        checked_at = time.monotonic()
                    if time.monotonic() - started > timeout:
                        raise ToolError("Codex 调用超时，已终止本次分析进程，成功断点保留。", 408)
                    size = os.fstat(output.fileno()).st_size
                    if size > MAX_EVENT_BYTES:
                        raise ToolError("Codex 返回结果超过大小上限，未接受结果。", 502)
                    if size > offset:
                        reader.seek(offset)
                        data = reader.read(size - offset)
                        offset = size
                        pending += data
                        lines = pending.split(b"\n")
                        pending = lines.pop()
                        for line in lines:
                            try:
                                event = json.loads(line)
                            except (ValueError, UnicodeError):
                                raise ToolError("Codex 返回结果格式无效，未接受结果。", 502) from None
                            if not isinstance(event, dict):
                                raise ToolError("Codex 返回结果格式无效，未接受结果。", 502)
                            kind = event.get("type")
                            if kind in {"error", "turn.failed"}:
                                raise _failure(event)
                            if kind in {"item.started", "item.updated", "item.completed"}:
                                item = event.get("item") or {}
                                if not isinstance(item, dict):
                                    raise ToolError("Codex 返回结果格式无效，未接受结果。", 502)
                                if item.get("type") == "error":
                                    if kind == "item.completed" and item.get("message") == _SAFE_STARTUP_DIAGNOSTIC:
                                        continue
                                    raise _failure({"error": item})
                                if item.get("type") not in {"agent_message", "reasoning"}:
                                    raise ToolError("Codex 请求了未授权工具，已终止进程，未接受结果。", 403)
                                if kind == "item.completed" and item.get("type") == "agent_message":
                                    final = item.get("text")
                            elif kind == "turn.completed":
                                completed, usage = True, event.get("usage") or {}
                                if not isinstance(usage, dict):
                                    raise ToolError("Codex 返回结果格式无效，未接受结果。", 502)
                            elif kind not in {"thread.started", "turn.started"}:
                                raise ToolError("Codex 返回未支持的事件，未接受结果。", 502)
                    if process.poll() is not None:
                        if os.fstat(output.fileno()).st_size > offset:
                            continue
                        break
                    time.sleep(.05)
                if process.returncode != 0:
                    raise ToolError("本机 Codex 调用失败，请检查登录、版本或额度；成功断点保留。", 502)
                if not completed or pending or not isinstance(final, str):
                    raise ToolError("Codex 调用未正常完成，未接受结果。", 502)
                try:
                    answer = json.loads(final)["answer"]
                except (KeyError, TypeError, ValueError):
                    raise ToolError("Codex 返回结果格式无效，未接受结果。", 502) from None
                if not isinstance(answer, str) or not answer.strip():
                    raise ToolError("Codex 未返回有效结果。", 502)
                safe_usage = {target: value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
                              for target, value in (("input_tokens", usage.get("input_tokens")), ("output_tokens", usage.get("output_tokens")),
                                                    ("cached_tokens", usage.get("cached_input_tokens")))}
                limited = len(answer.encode()) > budget or (safe_usage["output_tokens"] or 0) >= max_output
                before_send()
                return {"text": answer.strip(), "usage": safe_usage, "output_limited": limited,
                        "runtime": {"kind": "codex-local", "version": state["version"], "model": model}}
            except (OSError, UnicodeError):
                raise ToolError("本机 Codex 进程通信失败，未接受结果。", 502) from None
            finally:
                _stop(process)
