"""The vault follows Raw -> Wiki -> area map -> project output."""
from __future__ import annotations

SOURCE_ROOT = "00-Inbox/代码资料"
WIKI_ROOT = "01-Wiki/业务知识"
MAP_ROOT = "02-Areas/业务地图"
PROJECT_ROOT = "03-Projects/代码业务分析"


def source_prefix(repo_id):
    return f"{SOURCE_ROOT}/{repo_id}"


def source_note_path(repo_id, filename):
    return f"{source_prefix(repo_id)}/{filename}"


def business_paths(repo_id):
    return {"overview": f"{MAP_ROOT}/{repo_id}/业务地图.md",
            **{key: f"{WIKI_ROOT}/{repo_id}/{key}.md" for key in ("entities", "flows", "rules", "data")}}


def summary_path(repo_id):
    return f"{PROJECT_ROOT}/{repo_id}/分析总结.md"


def repository_id_from_path(path):
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
