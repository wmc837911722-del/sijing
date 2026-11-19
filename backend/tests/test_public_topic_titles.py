import copy
import json

import pytest

from businessmap import public_topic_titles as titles
from businessmap.providers import ToolError
from test_full_analysis import full_project
from test_semantic_integration import semantic_pipeline


def topic(oid, title="方案配置与印章归档", scope="方案配置数量"):
    finding, excluded = oid + "-finding", oid + "-scope"
    operation = {"id": oid, "title": title, "category": "rules", "aliases": [title], "purpose": "使用方案配置设置门店数量。",
                 "summary": "门店数量有上限。", "fact_ids": [finding, excluded], "support_ids": [finding]}
    claim = {"operation_id": oid, "scope": scope, "text": "门店数量有上限。", "conditions": [],
             "section": "conditions", "support_ids": [finding]}
    return operation, [claim], {
        "inputs": {finding: {"text": "门店数量有上限。"}, excluded: {"text": "印章归档仅用于核对来源范围。"}},
        "decisions": {finding: {"parts": [{"outcome": "claim"}]}, excluded: {"parts": [{"outcome": "source_scope"}]}}}


def test_reviewed_operation_includes_the_public_subjects_and_excludes_only_private_material():
    op, claims, coverage = topic("operation:one")
    assert titles._reviewed_names(op, claims, coverage) == ["使用方案配置设置门店数量"]
    assert titles._reviewed_names({**op, "title": "方案配置、门店数量与印章"}, claims, coverage) == ["使用方案配置设置门店数量"]
    assert titles._reviewed_names({**op, "title": "方案配置（印章）"}, claims, coverage) == ["使用方案配置设置门店数量"]
    assert titles._reviewed_names({**op, "title": "报名审核"}, claims, coverage) is None
    assert titles._reviewed_names(op, [{**claims[0], "text": "印章归档须一并办理。"}], coverage) is None
    assert titles._reviewed_names({**op, "support_ids": list(op["fact_ids"])}, claims, coverage) == ["使用方案配置设置门店数量"]
    inverse = {**op, "title": "成员管理与禁止取消", "purpose": "成员管理支持取消禁止发言的设置。"}
    data = copy.deepcopy(coverage)
    data["inputs"][op["fact_ids"][1]]["text"] = "禁止取消仅为来源范围。"
    assert titles._reviewed_names(inverse, claims, data) == ["成员管理支持取消禁止发言的设置"]
    assert titles._short_heading("LPT1") is None and titles._short_heading("方案配置/数量") is None
    assert titles._short_heading("先配置方案。然后查看。") is None
    mixed = copy.deepcopy(coverage)
    fid = op["fact_ids"][1]
    mixed["decisions"][fid]["parts"] += [{"outcome": "claim", "quote": "核对来源范围"}]
    assert titles._reviewed_names(op, claims, mixed) is None
    mixed["decisions"][fid]["parts"][0]["quote"] = "印章归档"
    assert titles._reviewed_names(op, claims, mixed) == ["使用方案配置设置门店数量"]


def make_projection(*topics):
    from businessmap.business_semantics import _render_overviews
    operations = [t[0] for t in topics]
    docs = [{"key": "topic:" + op["id"].split(":")[1], "category": op["category"], "title": op["title"]}
            for op in operations]
    coverage = {"catalogue": operations, "claims": [c for _, claims, _ in topics for c in claims],
                "inputs": {k: v for _, _, data in topics for k, v in data["inputs"].items()},
                "decisions": {k: v for _, _, data in topics for k, v in data["decisions"].items()}}
    overview, summary = _render_overviews("业务项目", operations)
    return {"public_documents": docs, "semantic_coverage": coverage, "overview_body": overview, "summary_body": summary}


def test_collisions_and_alternatives_are_scoped_to_the_destination_category():
    one = topic("operation:one")
    two = topic("operation:two", title="方案配置与印章归档历史")
    two[0]["aliases"].append("印章归档")
    one[0]["summary"] = "方案配置数量"
    two[0]["summary"] = "方案配置数量"
    with pytest.raises(ToolError):
        titles.public_topic_presentation(make_projection(one, two), "业务项目")
    two[0]["category"] = "flows"
    docs, _, _ = titles.public_topic_presentation(make_projection(one, two), "业务项目")
    assert docs[0]["title"] == docs[1]["title"] == "使用方案配置设置门店数量"
    two[0].update(category="rules", purpose="方案配置数量")
    docs, _, _ = titles.public_topic_presentation(make_projection(one, two), "业务项目")
    assert [doc["title"] for doc in docs] == ["使用方案配置设置门店数量", "方案配置数量"]


def test_unique_assignment_can_need_two_reassignments():
    topics = [topic("operation:" + key, title="方案配置与印章归档" + suffix)
              for key, suffix in (("a", ""), ("b", "历史"), ("c", "状态"))]
    for op, _, _ in topics:
        op["aliases"].append("印章归档")
    x, y, z = "门店方案配置", "方案配置", "中心门店方案配置"
    for (op, _, _), (purpose, summary) in zip(topics, [(x, y), (z, x), (z, z)]):
        op.update(purpose=purpose, summary=summary)
    docs, _, _ = titles.public_topic_presentation(make_projection(*topics), "业务项目")
    assert [d["title"] for d in docs] == [y, x, z]


def test_publish_uses_reviewed_purpose_and_replays_saved_results(full_project, monkeypatch):
    full, service, project, calls = semantic_pipeline(full_project, monkeypatch)
    original_complete = service.providers.complete
    catalogue_title, public_title = "方案配置与印章归档", "使用方案配置设置门店数量"

    def complete(system, user, **options):
        response = original_complete(system, user, **options)
        request, answer = json.loads(user), json.loads(response["text"])
        stage = request.get("stage")
        if "files" in request:
            answer["summary"] = "印章归档仅用于核对来源范围。"
        elif stage == "catalogue":
            answer["operations"][0]["title"] = catalogue_title
        elif stage == "proposal":
            answer["operations"][0]["purpose"] = "使用方案配置设置门店数量。"
            for claim in answer["claims"]:
                claim["scope"] = "方案配置数量"
        return {**response, "text": json.dumps(answer, ensure_ascii=False)}

    monkeypatch.setattr(service.providers, "complete", complete)
    assert full.analyze(project["id"])["publication_complete"] is True
    def state():
        return service.store.config_get("project_analysis:" + project["id"])["notes"]
    key, = [key for key in state() if key.startswith("topic:")]
    path = service.store.vault / state()[key]["path"]
    assert path.stem == public_title
    projection = copy.deepcopy(full._load(project["id"])["synthesis"])
    assert projection["public_documents"][0]["title"] == catalogue_title

    def paid_rows():
        with service.store._db() as db:
            return list(db.execute("SELECT * FROM business_semantic_tasks ORDER BY stage_hash"))

    ledger, paid_calls = paid_rows(), list(calls)
    assert full.analyze(project["id"])["publication_complete"] is True
    assert path.read_text().count("# " + public_title) == 1
    for index in ("overview", "summary"):
        body = (service.store.vault / state()[index]["path"]).read_text()
        assert public_title + "：" in body
        assert state()[key]["path"][:-3] + "|" + public_title + "]]" in body
        assert catalogue_title + "：" not in body
    assert state()[key]["path"] == path.relative_to(service.store.vault).as_posix()
    assert full._load(project["id"])["synthesis"] == projection and paid_rows() == ledger and calls == paid_calls
    human = path.read_text() + "\n人工说明\n"
    path.write_text(human)
    assert full.analyze(project["id"])["publication_complete"] is False
    assert path.read_text() == human and calls == paid_calls
