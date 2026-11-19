"""Render topic names from a verified semantic projection without changing its receipts."""
from collections import defaultdict, deque
import re
import unicodedata

from .business_semantics import _render_operation, _render_overviews
from .content_layout import public_component
from .core import slug
from .providers import ToolError


def _name_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def _short_heading(text):
    # Keep the entire approved phrase. Never cut a qualification or first sentence.
    name = text.strip().removesuffix("。").removesuffix(".")
    if not name or len(name) > 60 or re.search(r"[。！？；;!?\n]", name) or slug(name) != name:
        return None
    try:
        return public_component(name)
    except ToolError:
        return None


def _title_parts(name):
    return [part.strip() for part in re.split(r"以及|[、，,；;：:（）()【】与及和]", name) if part.strip()]


def _reviewed_names(op, claims, coverage):
    excluded = []
    visible = [op["purpose"], op["summary"]]
    supports = set(op["support_ids"])
    for claim in claims:
        visible.extend((claim["text"], claim["scope"], *claim["conditions"]))
        supports.update(claim["support_ids"])
    for fid in set(op["fact_ids"]) | supports:
        parts = coverage["decisions"][fid]["parts"]
        private = [p for p in parts if p["outcome"] in {"technical", "source_scope"}]
        text = coverage["inputs"][fid]["text"]
        if fid in op["fact_ids"]:
            excluded.extend(p["quote"] for p in private if p.get("quote"))
            if len(private) == len(parts):
                excluded.append(text)
        if fid in supports:
            visible.extend(p["quote"] for p in parts if p not in private and p.get("quote"))
            if not private:
                visible.append(text)
    if not excluded:
        return None
    title = op["title"]
    if any(title in text for text in visible):
        return None
    # Aliases are private navigation, never evidence. They can identify a
    # complete business label only when that exact wording occurs in the
    # title and in an explicitly excluded fact or quote.
    labels = {part for name in [title, *op["aliases"]] for part in _title_parts(name) if part in title}
    labels.update(text for text in excluded if text and text in title)
    if not any(any(label in text for text in excluded) and not any(label in text for text in visible)
               for label in labels):
        return None
    named = [p for p in _title_parts(title) if any(p in text for text in visible)]

    # The operation-level purpose was reviewed against all its claims. A claim
    # scope alone may describe an actor or a single detail rather than the topic.
    candidates = [op["purpose"], op["summary"]]
    return list(dict.fromkeys(name for text in candidates if (name := _short_heading(text))
                              and all(part in name for part in named)))


def public_topic_presentation(projection, project_name):
    """Return the public view; the accepted catalogue and projection hash stay intact."""
    documents = projection["public_documents"]
    coverage = projection["semantic_coverage"]
    claims = defaultdict(list)
    for claim in coverage["claims"]:
        claims[claim["operation_id"]].append(claim)
    operations = [op for op in coverage["catalogue"] if claims[op["id"]]]
    options = {}
    for op in operations:
        names = _reviewed_names(op, claims[op["id"]], coverage)
        if names is not None:
            options[op["id"]] = names
    reserved = {(op["category"], _name_key(op["title"])) for op in operations}
    categories = {op["id"]: op["category"] for op in operations}
    choices = {oid: {(categories[oid], _name_key(name)): name for name in reversed(names)
                     if (categories[oid], _name_key(name)) not in reserved} for oid, names in options.items()}
    # Prefer the whole reviewed purpose over its summary. If two topics offer
    # exactly the same descriptions there is no evidence they distinguish them.
    preferred = {oid: tuple(reversed(names)) for oid, names in choices.items()}
    if len(set(preferred.values())) != len(preferred):
        raise ToolError("多个主题的已复核说明完全相同，无法区分公开文档名称。", 409)

    owner, assigned = {}, {}
    for start in sorted(choices, key=lambda oid: len(choices[oid])):
        pending, previous = deque([start]), {start: None}
        while pending:
            oid = pending.popleft()
            available = next((name for name in preferred[oid] if name not in owner), None)
            if available is not None:
                while oid is not None:
                    owner[available] = oid
                    available, assigned[oid] = assigned.get(oid), available
                    oid = previous[oid]
                break
            for name in preferred[oid]:
                current = owner[name]
                if current not in previous:
                    previous[current] = oid
                    pending.append(current)
        else:
            raise ToolError("已复核的业务说明未提供可区分的公开主题名称，未写入本次文档。", 409)
    renamed = {oid: choices[oid][choice] for oid, choice in assigned.items()}
    if not renamed and len(operations) == len(coverage["catalogue"]):
        return documents, projection["overview_body"], projection["summary_body"]

    public = [{**op, "title": renamed[op["id"]]} if op["id"] in renamed else op for op in operations]
    by_id = {op["id"]: op for op in public}
    rendered = []
    for document in documents:
        oid = "operation:" + document["key"].partition(":")[2]
        if oid in renamed:
            document = {**document, "title": renamed[oid], "body": _render_operation(by_id[oid], claims[oid])}
        rendered.append(document)
    legacy = projection["summary_body"] == _render_overviews(project_name, coverage["catalogue"], _legacy_boundary=True)[1]
    return rendered, *_render_overviews(project_name, public, _legacy_boundary=legacy)
