"""Codex subprocess contracts, using an executable fixture and no account/model calls."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from businessmap.core import Store
from businessmap.providers import ProviderService, ToolError, provider_destination


@pytest.fixture
def service(tmp_path, monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_keyring", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    store = Store(tmp_path / "data")
    yield ProviderService(store)
    store.close()


@pytest.fixture
def cli(tmp_path, monkeypatch):
    from businessmap import codex_provider
    executable = tmp_path / "codex"
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
if '--version' in args:
    print('codex-cli 0.163.0-alpha.10.2')
elif args == ['login', 'status']:
    print('Logged in using ChatGPT', file=sys.stderr)
elif args == ['debug', 'models', '--bundled']:
    print('bad catalog' if (root / 'bad-catalog').exists() else json.dumps({'models':[{'slug':'fixture-direct','tool_mode':'direct','apply_patch_tool_type':'freeform','priority':0}]}))
else:
    if (root / 'mode').exists() and (root / 'mode').read_text() == 'not-reading':
        time.sleep(5)
    prompt = sys.stdin.read()
    (root / 'invocation.json').write_text(json.dumps({'args': args, 'stdin': prompt, 'cwd': os.getcwd(), 'env': dict(os.environ)}))
    mode = (root / 'mode').read_text() if (root / 'mode').exists() else 'ok'
    if mode == 'metadata-direct':
        catalogs = [args[i+1].split('=',1)[1] for i,arg in enumerate(args) if arg == '-c' and args[i+1].startswith('model_catalog_json=')]
        pinned = json.loads(pathlib.Path(json.loads(catalogs[0])).read_text()) if catalogs else None
        if not pinned or any(model.get('tool_mode') != 'code_mode_only' or model.get('apply_patch_tool_type') is not None for model in pinned['models']):
            print(json.dumps({'type':'item.started','item':{'id':'unsafe','type':'command_execution','command':'source-driven access'}}),flush=True)
            time.sleep(5)
    if mode == 'timeout':
        time.sleep(5)
    elif mode == 'quota':
        print(json.dumps({'type': 'turn.failed', 'error': {'message': 'usage limit reached raw-secret'}}))
        sys.exit(1)
    elif mode == 'tool':
        print(json.dumps({'type': 'item.started', 'item': {'id': 'a', 'type': 'command_execution', 'command': 'raw-secret'}}), flush=True)
        time.sleep(5)
    elif mode == 'malformed':
        print('raw-secret')
    elif mode == 'huge':
        print('x' * 2000000)
    elif mode == 'unknown':
        print(json.dumps({'type': 'account.changed', 'message': 'raw-secret'}))
    elif mode == 'stderr':
        print('raw-secret', file=sys.stderr)
        sys.exit(1)
    else:
        print(json.dumps({'type': 'thread.started', 'thread_id': 'fixture'}))
        if mode == 'warning':
            print(json.dumps({'type': 'item.completed', 'item': {'id':'warning', 'type':'error', 'message':'Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`.'}}))
        print(json.dumps({'type': 'turn.started'}))
        print(json.dumps({'type': 'item.completed', 'item': {'id': 'a', 'type': 'agent_message', 'text': json.dumps({'answer': '{"title":"规则","summary":"已分析","findings":[],"unknowns":[]}'})}}))
        if mode != 'incomplete':
            usage = {'input_tokens': 110, 'cached_input_tokens': 10, 'output_tokens': 3000 if mode == 'limited' else 30}
            if mode.startswith('missing-'):
                usage.pop(mode.removeprefix('missing-'))
            print(json.dumps({'type': 'turn.completed', 'usage': 'raw-secret' if mode == 'bad-usage' else usage}))
''')
    executable.chmod(0o700)
    monkeypatch.setattr(codex_provider, "find_codex", lambda: str(executable))
    return tmp_path


def save(service, **extra):
    return service.save({"name": "本机 Codex", "kind": "codex-local", "model": "default", **extra})


def test_codex_setting_uses_local_login_without_api_credentials(service, monkeypatch):
    monkeypatch.setattr(service.secrets, "get", lambda _: (_ for _ in ()).throw(AssertionError("must not read API credentials")))
    selected = save(service)
    assert selected["base_url"] == "codex://local"
    assert selected["has_key"] is False
    assert selected["capabilities"] == {"complete": True, "tool_turn": False}
    assert "secret_ref" not in service.store.config_get("providers")[0]
    with pytest.raises(ToolError, match="密钥"):
        save(service, api_key="do-not-store")
    with pytest.raises(ToolError, match="本机"):
        save(service, base_url="https://untrusted.invalid")


def test_codex_complete_passes_only_stdin_in_isolated_tool_free_process(service, cli, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "never-inherit")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "never-inherit")
    monkeypatch.setenv("CODEX_THREAD_ID", "never-inherit")
    selected = save(service)
    checks = []
    result = service.complete("固定规则", "已脱敏源码", provider_id=selected["id"], json_mode=True,
                              expected_destination=provider_destination(selected), before_send=lambda: checks.append(True))
    assert json.loads(result["text"])["title"] == "规则"
    assert result["usage"] == {"input_tokens": 110, "output_tokens": 30, "cached_tokens": 10}
    assert result["output_limited"] is False
    assert service.usage()["requests"] == 1
    call = json.loads((cli / "invocation.json").read_text())
    assert "已脱敏源码" in call["stdin"]
    assert "已脱敏源码" not in " ".join(call["args"])
    assert call["cwd"] != str(service.store.data_dir)
    assert not Path(call["cwd"]).exists()
    assert not {"OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_THREAD_ID"} & call["env"].keys()
    assert {"--no-daemon", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--json", "--strict-config"}.issubset(call["args"])
    assert "read-only" in call["args"] and "never" in call["args"]
    config = [call["args"][i + 1] for i, value in enumerate(call["args"]) if value == "-c"]
    assert "features.shell_tool=false" in config
    assert "features.hooks=false" in config
    assert "features.plugins=false" in config
    assert "features.skip_host_skill_discovery=true" in config
    assert "features.code_mode_host={enabled=false,disable_in_process_fallback=true}" in config
    assert "features.code_mode_only=true" in config
    assert checks


@pytest.mark.parametrize("mode,code,phrase", [
    ("quota", 429, "额度"), ("tool", 403, "工具"), ("malformed", 502, "结果"),
    ("incomplete", 502, "完成"), ("huge", 502, "大小"), ("stderr", 502, "失败"),
    ("unknown", 502, "事件"), ("bad-usage", 502, "结果"),
])
def test_codex_rejects_unsafe_or_incomplete_results_without_leaking_output(service, cli, mode, code, phrase):
    (cli / "mode").write_text(mode)
    selected = save(service)
    with pytest.raises(ToolError) as error:
        service.complete("规则", "源码", provider_id=selected["id"], json_mode=True)
    assert error.value.status_code == code
    assert phrase in str(error.value)
    assert "raw-secret" not in str(error.value)
    assert service.usage()["requests"] == 0


def test_codex_timeout_stops_subprocess(service, cli, monkeypatch):
    from businessmap import codex_provider
    monkeypatch.setattr(codex_provider, "TIMEOUT_SECONDS", .15)
    (cli / "mode").write_text("timeout")
    selected = save(service)
    with pytest.raises(ToolError) as error:
        service.complete("规则", "源码", provider_id=selected["id"])
    assert error.value.status_code == 408


def test_codex_timeout_also_covers_child_that_never_reads_large_stdin(service, cli, monkeypatch):
    import time
    from businessmap import codex_provider
    monkeypatch.setattr(codex_provider, "TIMEOUT_SECONDS", .15)
    (cli / "mode").write_text("not-reading")
    selected = save(service)
    started = time.monotonic()
    with pytest.raises(ToolError) as error:
        service.complete("规则", "源" * 47000, provider_id=selected["id"])
    assert error.value.status_code == 408
    assert time.monotonic() - started < 2


def test_codex_guard_and_destination_changes_prevent_dispatch(service, cli):
    selected = save(service)
    wrong = {**provider_destination(selected), "model": "other"}
    with pytest.raises(ToolError, match="目标"):
        service.complete("规则", "源码", provider_id=selected["id"], expected_destination=wrong)
    def stopped():
        raise ToolError("用户暂停", 409)
    with pytest.raises(ToolError, match="用户暂停"):
        service.complete("规则", "源码", provider_id=selected["id"], before_send=stopped)
    assert not (cli / "invocation.json").exists()


def test_codex_explicitly_rejects_tool_turn_before_any_dispatch(service, cli):
    selected = save(service)
    with pytest.raises(ToolError, match="工具"):
        service.tool_turn("规则", [{"role": "user", "content": "数据问答"}], [], provider_id=selected["id"])
    assert not (cli / "invocation.json").exists()


def test_codex_reports_consumed_output_budget_without_accepting_it_as_unlimited(service, cli):
    (cli / "mode").write_text("limited")
    selected = save(service)
    result = service.complete("规则", "源码", provider_id=selected["id"], max_output=2800)
    assert result["output_limited"] is True


def test_codex_accepts_only_known_fail_closed_startup_diagnostic(service, cli):
    (cli / "mode").write_text("warning")
    selected = save(service)
    result = service.complete("规则", "源码", provider_id=selected["id"])
    assert json.loads(result["text"])["title"] == "规则"


def test_codex_direct_model_metadata_cannot_enable_local_tool_execution(service, cli):
    (cli / "mode").write_text("metadata-direct")
    selected = save(service)
    result = service.complete("规则", "源码", provider_id=selected["id"])
    assert json.loads(result["text"])["title"] == "规则"


def test_codex_unverified_catalog_prevents_any_source_dispatch(service, cli):
    (cli / "bad-catalog").touch()
    selected = save(service)
    with pytest.raises(ToolError, match="工具边界"):
        service.complete("规则", "源码", provider_id=selected["id"])
    assert not (cli / "invocation.json").exists()


def test_codex_pause_during_run_terminates_process_and_preserves_guard_reason(service, cli):
    (cli / "mode").write_text("timeout")
    selected = save(service)
    def guard():
        if (cli / "invocation.json").exists():
            raise ToolError("用户暂停", 409)
    with pytest.raises(ToolError, match="用户暂停"):
        service.complete("规则", "源码", provider_id=selected["id"], before_send=guard)
    assert service.usage()["requests"] == 0


@pytest.mark.parametrize("unknown_field", ["input_tokens", "output_tokens"])
@pytest.mark.parametrize("existing_usage", [False, True])
def test_codex_unknown_usage_remains_unknown_in_aggregate(service, cli, unknown_field, existing_usage):
    if existing_usage:
        service.store.config_set("provider_usage", {"requests": 3, "input_tokens": 100, "output_tokens": 50, "cached_tokens": 20})
    (cli / "mode").write_text("missing-" + unknown_field)
    selected = save(service)
    result = service.complete("规则", "源码", provider_id=selected["id"])
    assert result["usage"][unknown_field] is None
    assert service.usage()[unknown_field] is None
    (cli / "mode").unlink()
    service.complete("规则", "源码", provider_id=selected["id"])
    assert service.usage()[unknown_field] is None
    assert service.usage()["requests"] == (5 if existing_usage else 2)
    other = "output_tokens" if unknown_field == "input_tokens" else "input_tokens"
    expected = (110 if existing_usage else 60) if other == "output_tokens" else (320 if existing_usage else 220)
    assert service.usage()[other] == expected


def test_codex_unknown_followed_by_http_known_does_not_recover_unknown_usage(service, cli, monkeypatch):
    import httpx
    (cli / "mode").write_text("missing-input_tokens")
    selected = save(service)
    service.complete("规则", "源码", provider_id=selected["id"])
    http = service.save({"name": "HTTP", "kind": "openai-compatible", "model": "fixture", "base_url": "https://fixture.invalid"})
    original = httpx.Client
    def respond(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "规则"}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 200, "completion_tokens": 9,
                                                  "prompt_tokens_details": {"cached_tokens": 0}}})
    monkeypatch.setattr("businessmap.providers.httpx.Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    service.complete("规则", "源码", provider_id=http["id"])
    assert service.usage()["input_tokens"] is None
    assert service.usage()["output_tokens"] == 39
    (cli / "mode").unlink()
    service.complete("规则", "源码", provider_id=selected["id"])
    assert service.usage()["input_tokens"] is None
    assert service.usage()["output_tokens"] == 69
    assert service.usage()["requests"] == 3
