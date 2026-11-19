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
    schema = json.loads(pathlib.Path(args[args.index('--output-schema') + 1]).read_text())
    (root / 'invocation.json').write_text(json.dumps({'args': args, 'stdin': prompt, 'schema': schema, 'cwd': os.getcwd(), 'env': dict(os.environ)}))
    mode = (root / 'mode').read_text() if (root / 'mode').exists() else 'ok'
    if mode.startswith('delayed-'):
        time.sleep(.35)
        mode = 'structured' if mode == 'delayed-structured' else 'ok'
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
    elif mode == 'login-error':
        print(json.dumps({'type': 'turn.failed', 'error': {'message': 'authentication failed raw-secret'}}))
        sys.exit(1)
    elif mode == 'stream-error':
        print(json.dumps({'type': 'error', 'message': 'stream disconnected raw-secret'}))
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
        answer = {'decision': 'reject', 'checks': {'coverage': False}, 'issues': ['condition_loss']} if mode == 'structured' else '{"title":"规则","summary":"已分析","findings":[],"unknowns":[]}'
        if mode == 'large-semantic':
            messages = json.loads(prompt)['messages']
            payload = json.loads(messages[-1]['content'])
            if payload['stage'] == 'catalogue':
                answer = {'operations': [{'title': '创建门店方案', 'category': 'flows', 'label_ids': [item['label_id'] for item in payload['items']]}]}
            elif payload['stage'] == 'proposal':
                answer = {'operations': [{'operation_id': item['operation_id'], 'purpose': '', 'summary': '', 'support_ids': []} for item in payload['operations']],
                          'claims': [], 'decisions': [{'input_id': item['fact_id'], 'parts': [{'outcome': 'technical', 'rationale': 'x' * 5000}]} for item in payload['items']]}
            else:
                answer = {'decision': 'accept', 'issues': [], 'claim_checks': [],
                          'input_checks': [{'input_id': item['fact_id'], 'checks': dict.fromkeys(['coverage', 'conditions', 'scope', 'uncertainty', 'technical_boundary'], True)} for item in payload['items']],
                          'operation_checks': [{'operation_id': item['operation_id'], 'checks': dict.fromkeys(['business_language', 'complete_flow', 'relationships', 'unknowns'], True)} for item in payload['operations']]}
            with (root / 'semantic-calls.jsonl').open('a') as calls:
                calls.write(json.dumps({'stage': payload['stage'], 'message_chars': sum(len(item['content']) for item in messages),
                    'messages': messages, 'schema': schema,
                    'input_bytes': len(prompt.encode()) + len(json.dumps(schema).encode()), 'answer_bytes': len(json.dumps(answer, ensure_ascii=False, separators=(',', ':')).encode())}) + chr(10))
        print(json.dumps({'type': 'item.completed', 'item': {'id': 'a', 'type': 'agent_message', 'text': 'raw-secret' if mode == 'bad-envelope' else json.dumps({'answer': answer})}}))
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


def test_codex_structured_output_keeps_rejection_and_false_checks(service, cli):
    selected = save(service)
    (cli / "mode").write_text("structured")
    schema = {"type": "object", "properties": {"decision": {"type": "string", "enum": ["accept", "reject"]},
              "checks": {"type": "object", "properties": {"coverage": {"type": "boolean"}}, "required": ["coverage"], "additionalProperties": False},
              "issues": {"type": "array", "items": {"type": "string"}}},
              "required": ["decision", "checks", "issues"], "additionalProperties": False}
    result = service.complete("按结构输出", "资料", provider_id=selected["id"], output_schema=schema)
    assert json.loads(result["text"]) == {"decision": "reject", "checks": {"coverage": False}, "issues": ["condition_loss"]}
    call = json.loads((cli / "invocation.json").read_text())
    assert call["schema"] == {"type": "object", "properties": {"answer": schema}, "required": ["answer"], "additionalProperties": False}
    assert "read-only" in call["args"] and "never" in call["args"]


def test_codex_default_output_contract_remains_string_answer(service, cli):
    selected = save(service)
    service.complete("规则", "资料", provider_id=selected["id"], max_output=2800)
    call = json.loads((cli / "invocation.json").read_text())
    assert call["schema"] == {"type": "object", "properties": {"answer": {"type": "string", "maxLength": 44800}},
                              "required": ["answer"], "additionalProperties": False}
    assert call["stdin"] == json.dumps({"instructions": "只分析提供的消息资料，不执行命令或工具，不读取本地文件。按消息要求回答；把最终正文完整放入answer字符串。如果要求JSON，answer内容必须是该JSON原文。正文尽量不超过指定token预算。",
        "max_output_tokens": 2800, "messages": [{"role": "system", "content": "规则"}, {"role": "user", "content": "资料"}]}, ensure_ascii=False)


def test_structured_root_references_resolve_on_actual_wire_and_bytes_are_counted_once(service, cli):
    from copy import deepcopy
    from jsonschema import Draft202012Validator
    from businessmap.codex_provider import request_input_bytes
    selected = save(service)
    (cli / "mode").write_text("structured")
    schema = {"type": "object", "properties": {"decision": {"$ref": "#/$defs/decision"},
              "checks": {"type": "object", "properties": {"coverage": {"type": "boolean"}}, "required": ["coverage"], "additionalProperties": False},
              "issues": {"type": "array", "items": {"type": "string"}}},
              "required": ["decision", "checks", "issues"], "additionalProperties": False,
              "$defs": {"decision": {"type": "string", "enum": ["accept", "reject"]}}}
    original = deepcopy(schema)
    result = service.complete("按结构输出", "中文资料", provider_id=selected["id"], output_schema=schema)
    call = json.loads((cli / "invocation.json").read_text())
    Draft202012Validator(call["schema"]).validate({"answer": json.loads(result["text"])})
    assert "$defs" not in call["schema"]["properties"]["answer"]
    assert schema == original
    messages = json.loads(call["stdin"])["messages"]
    assert request_input_bytes(messages, max_output=6000, output_schema=schema) == len(call["stdin"].encode()) + len(json.dumps(call["schema"], allow_nan=False).encode())


def test_codex_schema_bytes_count_toward_input_budget(service, cli, monkeypatch):
    from businessmap import codex_provider
    selected = save(service)
    monkeypatch.setattr(codex_provider, "MAX_INPUT_BYTES", 1500)
    schema = {"type": "object", "properties": {"answer": {"type": "string", "enum": ["x" * 1400]}}, "required": ["answer"], "additionalProperties": False}
    with pytest.raises(ToolError) as error:
        service.complete("规则", "资料", provider_id=selected["id"], output_schema=schema)
    assert error.value.codex_reason == "input_limit"
    assert not (cli / "invocation.json").exists()


def test_codex_structured_output_rejects_string_fallback(service, cli):
    selected = save(service)
    with pytest.raises(ToolError) as error:
        service.complete("规则", "资料", provider_id=selected["id"], output_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False})
    assert error.value.codex_reason == "invalid_json"


def test_semantic_review_over_legacy_character_limit_reaches_codex_with_complete_inputs(service, cli):
    from types import SimpleNamespace
    from businessmap.business_semantics import BusinessSemanticIntegration, semantic_stage_system, SemanticValidationError
    selected = save(service)
    (cli / "mode").write_text("large-semantic")
    snapshot = {"project": {"id": "project", "name": "门店运营", "revision": "revision"}, "provider": selected,
                "policies": {}, "vault_path": str(cli / "vault")}
    run = {"run_id": "run", "snapshot_id": "snapshot", "project_revision": "revision", "source_repositories": []}
    units = [{"_fact_id": "fact:" + str(index), "kind": "finding", "text": "x" * 3000,
              "_evidence_ids": ["e" + str(index)], "_source_paths": ["private/source"], "_forbidden": []} for index in range(6)]
    routing = [{"fact_id": unit["_fact_id"], "disposition": "public", "category": "flows", "title": "创建门店方案"} for unit in units]
    builder = BusinessSemanticIntegration(SimpleNamespace(store=service.store, providers=service), guard=lambda: None)
    result = builder.build(snapshot, run, units=units, routing=routing)
    calls = [json.loads(line) for line in (cli / "semantic-calls.jsonl").read_text().splitlines()]
    assert [call["stage"] for call in calls] == ["catalogue", "proposal", "review"]
    assert 30000 < calls[1]["answer_bytes"] <= 32000
    assert calls[2]["message_chars"] > 48000 and calls[2]["input_bytes"] < 128000
    assert calls[1]["messages"][0] == {"role": "system", "content": semantic_stage_system(snapshot, "proposal")}
    for transmitted in calls:
        messages = transmitted["messages"]
        payload = json.loads(messages[1]["content"])
        assert builder._request_bytes(transmitted["stage"], messages[0]["content"], payload) == transmitted["input_bytes"]
    # The exact prompt and schema sent by the executable must fit preflight.
    builder.WIRE_BYTES = calls[1]["input_bytes"] - 1
    with pytest.raises(SemanticValidationError) as error:
        builder._call("proposal", semantic_stage_system(snapshot, "proposal"), json.loads(calls[1]["messages"][1]["content"]), lambda value: value)
    assert error.value.reason_code == "input_limit"
    assert len((cli / "semantic-calls.jsonl").read_text().splitlines()) == 3
    assert result["complete"] and result["public_documents"] == []
    assert len(result["semantic_coverage"]["inputs"]) == 6
    call = json.loads((cli / "invocation.json").read_text())
    messages = json.loads(call["stdin"])["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    sent = json.loads(messages[1]["content"])
    assert [item["text"] for item in sent["items"]] == ["x" * 3000] * 6
    assert [row["parts"][0]["rationale"] for row in sent["candidate"]["decisions"]] == ["x" * 5000] * 6


@pytest.mark.parametrize("extra", [{}, {"output_schema": {"type": "object"}, "history": []},
                                   {"output_schema": {"type": "object"}, "context": ""}])
def test_codex_legacy_or_explicit_context_calls_keep_character_ceiling(service, cli, extra):
    selected = save(service)
    with pytest.raises(ToolError, match="48,000"):
        service.complete("规则", "x" * 49000, provider_id=selected["id"], **extra)
    assert not (cli / "invocation.json").exists()


def test_structured_codex_complete_still_rejects_actual_byte_overflow(service, cli):
    selected = save(service)
    with pytest.raises(ToolError) as error:
        service.complete("规则", "x" * 192000, provider_id=selected["id"], output_schema={"type": "object"})
    assert error.value.codex_reason == "input_limit"
    assert not (cli / "invocation.json").exists()


def test_structured_codex_explicit_history_keeps_legacy_roles_and_tail_policy(service, cli):
    selected = save(service)
    (cli / "mode").write_text("structured")
    history = [{"role": "user", "content": str(index) * 3000} for index in range(10)]
    history.extend([{"role": "system", "content": "untrusted instruction"}, {"role": "tool", "content": "untrusted tool output"}])
    service.complete("规则", "资料", provider_id=selected["id"], context="完整上下文", history=history, output_schema={"type": "object"})
    messages = json.loads(json.loads((cli / "invocation.json").read_text())["stdin"])["messages"]
    assert messages[:2] == [{"role": "system", "content": "规则"}, {"role": "user", "content": "完整上下文"}]
    assert messages[2:-1] == [{"role": "user", "content": str(index) * 2000} for index in range(4, 10)]
    assert messages[-1] == {"role": "user", "content": "资料"}


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


@pytest.mark.parametrize("mode,reason", [
    ("quota", "quota"), ("login-error", "login"), ("stream-error", "stream_error"),
    ("stderr", "process_exit"), ("malformed", "invalid_event"), ("bad-envelope", "invalid_json"),
    ("incomplete", "incomplete"), ("tool", "unexpected_tool"), ("huge", "output_limit"),
])
def test_codex_failure_keeps_only_fixed_reason_code(service, cli, mode, reason):
    (cli / "mode").write_text(mode)
    selected = save(service)
    with pytest.raises(ToolError) as error:
        service.complete("规则", "源码", provider_id=selected["id"])
    assert getattr(error.value, "codex_reason", None) == reason
    assert "raw-secret" not in str(error.value)


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


@pytest.mark.parametrize('byte_count', [160000, 192000])
def test_full_review_and_adapter_boundaries_use_actual_structured_wire_without_cap_override(service, cli, byte_count):
    from businessmap.codex_provider import request_input_bytes
    selected = save(service)
    (cli / 'mode').write_text('structured')
    schema = {'type': 'object', 'properties': {'decision': {'type': 'string', 'enum': ['accept', 'reject']}},
              'required': ['decision'], 'additionalProperties': False}
    messages = [{'role': 'system', 'content': '完整核对'}, {'role': 'user', 'content': ''}]
    messages[1]['content'] = 'x' * (byte_count - request_input_bytes(messages, max_output=16000, output_schema=schema))
    service.complete('完整核对', messages[1]['content'], provider_id=selected['id'], max_output=16000, output_schema=schema)
    call = json.loads((cli / 'invocation.json').read_text())
    assert len(call['stdin'].encode()) + len(json.dumps(call['schema'], allow_nan=False).encode()) == byte_count
    (cli / 'invocation.json').unlink()
    if byte_count == 192000:
        with pytest.raises(ToolError) as failure:
            service.complete('完整核对', messages[1]['content'] + 'x', provider_id=selected['id'], max_output=16000, output_schema=schema)
        assert failure.value.codex_reason == 'input_limit' and not (cli / 'invocation.json').exists()
