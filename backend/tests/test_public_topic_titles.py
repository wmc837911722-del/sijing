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


@pytest.mark.parametrize(("subject", "scope", "purpose"), [
    ("方案明细查看、受限编辑", "方案受限编辑的保存规则", "查看方案明细，按允许范围编辑方案。"),
    ("套餐维护与分配统计", "套餐的分配统计", "维护套餐及权限范围，查看套餐分配比例。"),
    ("比赛记录与操作日志查询", "操作日志查询", "查询和导出比赛记录，并查询操作日志。"),
])
def test_verified_purpose_can_name_reviewed_scopes_without_copying_the_catalogue(subject, scope, purpose):
    op, claims, data = topic("operation:one", title=subject + "与印章归档", scope=scope)
    if "受限编辑" in scope:
        claims[0].update(text="按允许编辑的内容保存方案。", conditions=["按允许编辑的内容保存方案时"])
    elif "分配统计" in scope:
        claims[0].update(text="各套餐分配占比以已分配的门店数为分母。", conditions=["核对各套餐分配占比时"])
    else:
        claims[0].update(text="操作日志支持分页查询。", conditions=["查询操作日志时"])
    op.update(purpose=purpose, summary="。".join(["已核对本操作适用的条件与范围"] * 7))
    if "分配统计" in scope:
        op["summary"] = "分配比例按分配总数计算；相关权限在主题中说明。"
    projection = make_projection((op, claims, data))
    original = copy.deepcopy(projection)
    docs, overview, summary = titles.public_topic_presentation(projection, "业务项目")
    assert docs[0]["title"] == purpose[:-1]
    assert purpose[:-1] + "：" in overview and purpose[:-1] + "：" in summary
    assert projection == original


@pytest.mark.parametrize("protected", ["仅经办人可撤回", "未报名成员", "每单最多五件", "仅在到期前可变更"])
def test_reviewed_purpose_cannot_remove_an_explicit_catalogue_qualification(protected):
    op, claims, data = topic("operation:one", title=protected + "与印章归档", scope=protected)
    op.update(purpose="查看申请详情，维护相关信息。", summary="公开规则与适用范围在主题中说明。")
    with pytest.raises(ToolError) as exc:
        titles.public_topic_presentation(make_projection((op, claims, data)), "业务项目")
    assert exc.value.status_code == 409


@pytest.mark.parametrize(("part", "scope", "claim", "condition", "purpose"), [
    ("甲方授权乙方", "甲方授权乙方", "乙方授权甲方后，甲方可以授权乙方查阅资料。", "乙方授权甲方时", "乙方授权甲方查阅资料。"),
    ("受限编辑", "方案受限编辑的保存规则", "允许编辑的内容仅在授权范围内保存。", "按允许编辑的内容保存方案时", "查看方案明细，编辑方案。"),
    ("退款申请审核", "门店退款申请审核", "门店可提交退款申请，审核通过的申请不能撤回。", "门店核对退款申请时", "门店退款申请撤回。"),
    ("退款申请", "商户的退款申请", "商户退款申请需要审核。", "商户核对退款申请时", "商户退款审核。"),
    ("监护人本人查询", "监护人本人查询", "工作人员查询监护人本人时，只能查看监护人本人查询过的申请。", "查询监护人本人时", "查询监护人本人。"),
    ("操作日志查询", "操作日志查询", "允许查询操作日志。", "查询操作日志时", "禁止查询操作日志。"),
    ("总部复核", "总部复核", "总部在门店初审后复核申请。", "门店审批完成时", "维护门店资料，复核门店申请。"),
])
def test_restatement_must_preserve_actor_and_limit(part, scope, claim, condition, purpose):
    op, claims, data = topic("operation:one", title=part + "与印章归档", scope=scope)
    claims[0].update(text=claim, conditions=[condition])
    op.update(purpose=purpose, summary="主题中说明各环节的业务规则。")
    with pytest.raises(ToolError) as exc:
        titles.public_topic_presentation(make_projection((op, claims, data)), "业务项目")
    assert exc.value.status_code == 409


def test_missing_supported_headings_are_distinct_from_actual_name_collisions():
    topics = [topic("operation:" + key, title=scope + "与印章归档", scope=scope)
              for key, scope in (("one", "仅经办人可撤回"), ("two", "未报名成员"))]
    with pytest.raises(ToolError, match="未提供可区分的公开主题名称"):
        titles.public_topic_presentation(make_projection(*topics), "业务项目")


@pytest.mark.parametrize(("part", "scope", "purpose", "summary", "statement", "condition"), [
    ("操作日志查询", "操作日志查询", "查询操作日志。", "只提供导出。", "操作日志支持导出，不支持查询。", "查询操作日志时"),
    ("分配统计", "套餐的分配统计", "查看套餐分配比例。", "分配比例按总量计算。", "套餐分配只提供数量，其他事项占50%。", "核对套餐分配数量时"),
    ("分配统计", "套餐的分配统计", "查看套餐分配比例。", "暂不提供分配比例。", "套餐分配只提供数量。", "核对套餐分配数量时"),
    ("分配统计", "套餐的分配统计", "查看套餐分配比例。", "分配比例按总量计算。", "套餐分配只提供数量。", "核对套餐分配数量时"),
    ("受限编辑", "方案受限编辑的保存规则", "按禁止范围编辑方案。", "应排除不能编辑的内容。", "排除禁止编辑的内容。", "按禁止编辑的内容剔除不可保存的字段时"),
])
def test_heading_requires_positive_evidence_for_the_specific_measure_or_edit(part, scope, purpose, summary, statement, condition):
    op, claims, data = topic("operation:one", title=part + "与印章归档", scope=scope)
    op.update(purpose=purpose, summary=summary)
    claims[0].update(text=statement, conditions=[condition])
    with pytest.raises(ToolError) as exc:
        titles.public_topic_presentation(make_projection((op, claims, data)), "业务项目")
    assert exc.value.status_code == 409
