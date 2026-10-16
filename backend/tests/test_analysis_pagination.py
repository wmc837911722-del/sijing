from __future__ import annotations

import pytest

from businessmap.core import CoreError


def _original(result, key, title):
    return "".join(result["documents"][part][1].removeprefix(f"# {title} · 第{number}部分\n\n")
                   for number, part in enumerate(result["part_keys"], 1))


def test_small_document_keeps_exact_original_and_path():
    from businessmap.analysis_pagination import paginate_document
    body = "# 业务地图\n\n### 账号\n\n原有规则。\n"
    result = paginate_document("overview", "业务地图", body, [], "02-Areas/业务地图.md", 4096)
    assert result == {"documents": {"overview": ("业务地图", body, [])}, "paths": {"overview": "02-Areas/业务地图.md"}, "part_keys": []}


def test_map_preserves_all_business_domains_and_exact_original_in_bounded_pages():
    from businessmap.analysis_pagination import paginate_document
    names = ["账号", "租户", "权限", "报名", "赛事", "赛季", "比赛", "记分", "积分", "段位", "排行", "奖励", "宝箱", "成就", "支付"]
    body = "# 业务地图\n\n## 业务域与职责\n\n" + "\n\n".join(f"### {name}\n\n" + (name + "状态规则。") * 80 for name in names) + "\n"
    result = paginate_document("overview", "完整业务", body, [], "02-Areas/项目/业务地图.md", 4096)
    assert len(result["part_keys"]) > 2
    assert _original(result, "overview", "完整业务") == body
    homepage = result["documents"]["overview"][1]
    assert all(name in homepage for name in names)
    assert all(f"[[{result['paths'][part][:-3]}|第{number}部分]]" in homepage for number, part in enumerate(result["part_keys"], 1))
    assert all(len(document[1].encode()) <= 4096 for document in result["documents"].values())
    assert result["paths"]["overview:part:1"] == "02-Areas/项目/业务地图/第1部分.md"


def test_oversized_paragraph_splits_only_at_whole_lines_and_preserves_crlf():
    from businessmap.analysis_pagination import paginate_document
    body = "## 来源与流程\r\n" + "".join(f"- 完整流程 {index}：保留条件与状态。\r\n" for index in range(100))
    result = paginate_document("summary", "分析总结", body, ["repo/file.ts"], "03-Projects/分析总结.md", 1200)
    assert _original(result, "summary", "分析总结") == body
    assert len(result["part_keys"]) > 1
    assert all(result["documents"][part][1].endswith("\r\n") for part in result["part_keys"])
    assert all(result["documents"][part][2] == ["repo/file.ts"] for part in result["part_keys"])


def test_single_oversized_line_is_rejected_without_truncation():
    from businessmap.analysis_pagination import paginate_document
    with pytest.raises(CoreError) as error:
        paginate_document("overview", "地图", "## 业务\n" + "大" * 500, [], "业务地图.md", 1000)
    assert error.value.status_code == 413


def test_homepage_uses_real_business_headings_and_ignores_fenced_examples():
    from businessmap.analysis_pagination import paginate_document
    body = "# 地图\n\n## 业务域\n\n### 真业务\n\n```md\n### 代码里的假标题\n```\n\n" + "\n".join("业务说明。" * 30 for _ in range(25))
    result = paginate_document("overview", "地图", body, [], "地图.md", 1600)
    homepage = result["documents"]["overview"][1]
    assert "真业务" in homepage and "代码里的假标题" not in homepage
    assert _original(result, "overview", "地图") == body
    fallback = paginate_document("summary", "总结", "## 覆盖范围\n\n" + "\n".join("事实。" * 30 for _ in range(25)), [], "总结.md", 1600)
    assert "覆盖范围" in fallback["documents"]["summary"][1]


def test_homepage_index_overflow_is_explicit_instead_of_dropping_headings():
    from businessmap.analysis_pagination import paginate_document
    body = "\n\n".join(f"### 完整业务主题{index}：" + "名" * 50 + "\n\n" + "状态事实。" * 20 for index in range(40))
    with pytest.raises(CoreError, match="首页索引") as error:
        paginate_document("overview", "地图", body, [], "地图.md", 1500)
    assert error.value.status_code == 413


def test_multi_megabyte_business_map_is_lossless_and_all_pages_fit_note_limit():
    from businessmap.analysis_pagination import paginate_document
    rule = "- 保留状态、条件、范围和来源，不截断事实。" + "字" * 70 + "\n"
    body = "\n\n".join(f"### 具体业务域{index}\n\n" + rule * 2000 for index in range(15))
    assert len(body.encode()) > 7_000_000
    result = paginate_document("overview", "完整地图", body, [], "02-Areas/项目/业务地图.md", 1_000_000)
    assert len(result["part_keys"]) >= 8
    assert _original(result, "overview", "完整地图") == body
    assert all(f"具体业务域{index}" in result["documents"]["overview"][1] for index in range(15))
    assert all(len(document[1].encode()) <= 1_000_000 for document in result["documents"].values())
