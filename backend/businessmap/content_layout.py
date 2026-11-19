"""The vault follows Raw -> Wiki -> area map -> project output."""
from __future__ import annotations

SOURCE_ROOT = "00-Inbox/代码资料"
WIKI_ROOT = "01-Wiki/业务知识"
MAP_ROOT = "02-Areas/业务地图"
PROJECT_ROOT = "03-Projects/代码业务分析"

CATEGORY_NAMES = {"entities": "实体", "flows": "流程", "rules": "规则", "data": "数据口径"}


def public_component(value):
    from .business_content import validate_public_business_text
    from .providers import ToolError
    import re
    if (not isinstance(value, str) or not value.strip() or len(value) > 100
            or re.search(r'[\\/\x00-\x1f<>:"|?*\[\]#]', value) or value.strip().startswith(".")
            or value.endswith((" ", "."))):
        raise ToolError("业务文档名称无效，未发布。", 422)
    validate_public_business_text(value)
    return value.strip()


def public_project_paths(project_name):
    name = public_component(project_name)
    return {"overview": f"02-Areas/业务地图/{name}/业务地图.md",
            "summary": f"03-Projects/业务梳理/{name}/分析总结.md"}


def public_topic_path(project_name, category, title):
    from .providers import ToolError
    if category not in CATEGORY_NAMES:
        raise ToolError("业务主题分类无效，未发布。", 422)
    return f"01-Wiki/{public_component(project_name)}/{CATEGORY_NAMES[category]}/{public_component(title)}.md"


def source_prefix(repo_id):
    return f"{SOURCE_ROOT}/{repo_id}"


def source_note_path(repo_id, filename):
    return f"{source_prefix(repo_id)}/{filename}"


def business_paths(repo_id):
    return {"overview": f"{MAP_ROOT}/{repo_id}/业务地图.md",
            **{key: f"{WIKI_ROOT}/{repo_id}/{key}.md" for key in ("entities", "flows", "rules", "data")}}


def summary_path(repo_id):
    return f"{PROJECT_ROOT}/{repo_id}/分析总结.md"


def project_business_paths(project_id):
    return {"overview": f"{MAP_ROOT}/项目/{project_id}/业务地图.md",
            **{key: f"{WIKI_ROOT}/项目/{project_id}/{key}.md" for key in ("entities", "flows", "rules", "data")}}


def project_summary_path(project_id):
    return f"{PROJECT_ROOT}/项目/{project_id}/分析总结.md"


def project_id_from_path(path):
    for prefix in (WIKI_ROOT, MAP_ROOT, PROJECT_ROOT):
        if path.startswith(prefix + "/项目/"):
            parts = path[len(prefix) + len("/项目/"):].split("/")
            if len(parts) >= 2:
                return parts[0]
    return None


def repository_id_from_path(path):
    if project_id_from_path(path):
        return None
    for prefix in (SOURCE_ROOT, WIKI_ROOT, MAP_ROOT, PROJECT_ROOT, "代码索引", "业务地图"):
        if path.startswith(prefix + "/"):
            parts = path[len(prefix) + 1:].split("/")
            if len(parts) >= 2:
                return parts[0]
    return None


def is_source_document(note):
    path = note["path"]
    return (path.startswith(("00-Inbox/", "代码索引/")) or note.get("type") in {"code", "raw"})


def legacy_destination(repo_id, key, path, *, source=False):
    if source and path.startswith(f"代码索引/{repo_id}/"):
        return source_note_path(repo_id, path.split("/", 2)[2])
    if path.startswith(f"业务地图/{repo_id}/"):
        return summary_path(repo_id) if key == "summary" else business_paths(repo_id).get(key, path)
    return path
