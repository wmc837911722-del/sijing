"""Mocked wire contracts; these do not claim authenticated vendor availability."""
from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest

from businessmap.app import create_app
from businessmap.core import Store
from businessmap.providers import ProviderService, ToolError, _SECRET_MEMORY, parse_json_answer


@pytest.fixture(autouse=True)
def session_credentials(monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_keyring", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    yield
    _SECRET_MEMORY.clear()


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path)
    yield ProviderService(store)
    store.close()


def mock_vendor(monkeypatch, responder):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=responder(json.loads(request.content)))
    original = httpx.Client
    monkeypatch.setattr("businessmap.providers.httpx.Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return requests


def save(service, base, model="test-model", key="fixture-key", **extra):
    return service.save({"name": "Domestic provider", "kind": "openai-compatible", "base_url": base,
                         "model": model, "api_key": key, **extra})


@pytest.mark.parametrize("base,model", [
    ("https://api.moonshot.cn/v1", "kimi-k3"),
    ("https://api.deepseek.com", "deepseek-chat"),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("https://ark.cn-beijing.volces.com/api/v3", "ep-user-deployment-id"),
    ("https://open.bigmodel.cn/api/paas/v4", "glm-4.7"),
    ("https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3.2"),
    ("https://qianfan.baidubce.com/v2", "ernie-4.5-turbo-128k"),
    ("https://api.hunyuan.cloud.tencent.com/v1", "hunyuan-turbos-latest"),
    ("https://spark-api-open.xf-yun.com/v1", "generalv3.5"),
    ("https://api.minimax.cn/v1", "MiniMax-M2.5"),
])
def test_domestic_endpoints_use_selected_model_key_and_stable_prefix(service, monkeypatch, base, model):
    selected = save(service, base, model)
    other = save(service, "https://other.example.invalid/v1", key="other-key")
    service.activate(other["id"])
    requests = mock_vendor(monkeypatch, lambda _: {"choices": [{"message": {"content": "业务回答"}}], "usage": {}})
    result = service.complete("Stable policy", "Current question", provider_id=selected["id"],
                              context="Stable business evidence", max_output=1500)
    assert result["text"] == "业务回答"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == base + "/chat/completions"
    assert request.headers["authorization"] == "Bearer fixture-key"
    payload = json.loads(request.content)
    assert payload["model"] == model and payload["max_tokens"] == 1500
    assert payload["messages"] == [
        {"role": "system", "content": "Stable policy"},
        {"role": "user", "content": "Stable business evidence"},
        {"role": "user", "content": "Current question"},
    ]
    assert not {"temperature", "top_p", "thinking", "enable_thinking", "response_format"} & payload.keys()
    assert "fixture-key" not in json.dumps(service.list())
    assert "fixture-key" not in json.dumps(service.store.config_get("providers"))


@pytest.mark.parametrize("base", ["https://api.minimax.cn/v1", "https://api.minimax.io/v1", "https://api.minimaxi.com/v1"])
def test_minimax_separates_reasoning_from_executable_json(service, monkeypatch, base):
    save(service, base, "MiniMax-M2.5")
    answer = '{"sql":"SELECT COUNT(*) AS count FROM orders","clarification":null}'
    def vendor(payload):
        assert payload["reasoning_split"] is True
        return {"choices": [{"message": {"content": answer, "reasoning_content": "Private reasoning"}}]}
    mock_vendor(monkeypatch, vendor)
    result = service.complete("SQL policy", "Question")
    assert parse_json_answer(result["text"])["sql"] == "SELECT COUNT(*) AS count FROM orders"
    assert "Private reasoning" not in json.dumps(result)


def test_custom_compatible_gateway_receives_no_vendor_specific_parameters(service, monkeypatch):
    save(service, "https://gateway.example.invalid/api.minimax.cn/v1/chat/completions", "MiniMax-M2.5")
    requests = mock_vendor(monkeypatch, lambda _: {"choices": [{"message": {"content": "OK"}}]})
    service.complete("Policy", "Question")
    assert str(requests[0].url).endswith("/v1/chat/completions")
    assert set(json.loads(requests[0].content)) == {"model", "messages", "max_tokens"}


@pytest.mark.parametrize("path", ["/v1", "/v2", "/x2", "/v1/chat/completions"])
def test_spark_does_not_implicitly_search_business_material_online(service, monkeypatch, path):
    save(service, "https://spark-api-open.xf-yun.com" + path)
    requests = mock_vendor(monkeypatch, lambda _: {"choices": [{"message": {"content": "OK"}}]})
    service.complete("Business policy", "Company question")
    assert json.loads(requests[0].content)["tools"] == [{"type": "web_search", "web_search": {"enable": False}}]


@pytest.mark.parametrize("raw,expected_cache,expected_miss", [
    ({"prompt_tokens": 100, "completion_tokens": 8, "prompt_tokens_details": {"cached_tokens": 80}}, 80, None),
    ({"prompt_tokens": 100, "completion_tokens": 8, "prompt_tokens_details": {"cached_tokens": 0}}, 0, None),
    ({"prompt_tokens": 100, "completion_tokens": 8, "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 40}, 60, 40),
    ({"prompt_tokens": 100, "completion_tokens": 8}, None, None),
    ({"prompt_tokens": 100, "completion_tokens": 8, "prompt_tokens_details": {"cached_tokens": True}}, None, None),
])
def test_domestic_usage_records_only_reported_cache(service, monkeypatch, raw, expected_cache, expected_miss):
    save(service, "https://api.moonshot.cn/v1", "kimi-k3")
    mock_vendor(monkeypatch, lambda _: {"choices": [{"message": {"content": "OK"}}], "usage": raw})
    usage = service.complete("Policy", "Question")["usage"]
    assert usage == {"input_tokens": 100, "output_tokens": 8,
                     "cached_tokens": expected_cache, "cache_miss_tokens": expected_miss}
    assert service.usage() == {"requests": 1, "input_tokens": 100, "output_tokens": 8, "cached_tokens": expected_cache}


def test_reasoning_limited_probe_confirms_connection_but_normal_data_query_is_rejected(tmp_path, monkeypatch):
    app = create_app(tmp_path, "fixture-session-token-at-least-sixteen")
    response = {"choices": [{"finish_reason": "length", "message": {
        "content": None, "reasoning_content": "private reasoning that must not escape"}}],
        "usage": {"prompt_tokens": 18, "completion_tokens": 32, "prompt_tokens_details": {"cached_tokens": 0}}}
    requests = mock_vendor(monkeypatch, lambda _: response)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer fixture-session-token-at-least-sixteen"
        saved = client.post("/api/providers", json={"name": "Kimi", "kind": "openai-compatible",
                            "base_url": "https://api.moonshot.cn/v1", "model": "kimi-k3", "api_key": "fixture-key"}).json()
        probe = client.post(f"/api/providers/{saved['id']}/test")
        assert probe.status_code == 200 and probe.json()["ok"]
        assert "尚未验证完整回答" in probe.json()["message"]
        assert json.loads(requests[0].content)["max_tokens"] == 32
        with sqlite3.connect(tmp_path / "demo.sqlite") as db:
            before = list(db.iterdump())
        chat = client.post("/api/chat", json={"message": "订单总数", "mode": "data", "datasource_id": "demo"})
        assert chat.status_code == 502 and "未返回正文" in chat.json()["detail"]
        with sqlite3.connect(tmp_path / "demo.sqlite") as db:
            assert list(db.iterdump()) == before
        settings = client.get("/api/settings").json()
        assert settings["usage"] == {"requests": 2, "input_tokens": 36, "output_tokens": 64, "cached_tokens": 0}
        with sqlite3.connect(tmp_path / "workspace.sqlite") as db:
            assert db.execute("SELECT COUNT(*) FROM config WHERE key LIKE 'query_result:%'").fetchone() == (0,)
        assert "private reasoning" not in probe.text + chat.text + json.dumps(settings)


@pytest.mark.parametrize("finish,reasoning", [("stop", "reasoning"), ("length", ""), ("length", None)])
def test_probe_never_accepts_other_empty_or_malformed_answers(service, monkeypatch, finish, reasoning):
    save(service, "https://api.moonshot.cn/v1", "kimi-k3")
    mock_vendor(monkeypatch, lambda _: {"choices": [{"finish_reason": finish,
                                                     "message": {"content": None, "reasoning_content": reasoning}}]})
    with pytest.raises(ToolError, match="未返回可用文本"):
        service.complete("Reply OK", "Connection test", max_output=32, _connection_test=True)
