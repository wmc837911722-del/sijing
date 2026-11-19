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


def _scope_in_purpose(part, purpose, summary, claims):
    # Compare complete business objects, actions and qualifiers. These tests can
    # select a whole reviewed purpose; none of them supplies words for a title.
    if re.search(r"[0-9０-９]|仅|只|未|不|无|须|禁止|除|最[多少]|至[多少]|前|后|时|以上|以下", part):
        return False
    clauses = [clause.removeprefix("并且").removeprefix("并") for clause in re.split(r"[，、；。]", purpose)]
    for claim in claims:
        scope, conditions = claim["scope"], claim["conditions"]
        if part not in scope:
            continue
        # Keep an affirmative predicate and its object in the same statement.
        statements = re.split(r"[，,；;。]", claim["text"])
        for action in ("查询", "查看"):
            if part.endswith(action):
                obj = part.removesuffix(action)
                phrase = action + obj
                offered = statements[0]
                if (obj and phrase in clauses and offered.startswith(obj + "支持") and action in offered
                        and not re.search(r"未|不|无|禁止|除|待确定|待确认", offered) and part not in claim["text"]
                        and any(c.removesuffix("时") == phrase for c in conditions)):
                    return True
        # Some scopes qualify a catalogue action with its business object, e.g.
        # a subject followed by the name of a measure or constrained operation.
        subject = scope.partition(part)[0].removesuffix("预期").removesuffix("的")
        if not subject:
            continue
        if part.endswith("统计"):
            dimension = part.removesuffix("统计")
            series = subject + dimension
            for clause in clauses:
                mode, match, metric = clause.partition(series)
                if (not dimension or not match or mode not in {"查看", "统计", "展示", "计算", "汇总"}
                        or metric not in {"比例", "占比", "数量", "总数", "次数", "金额", "均值", "比率"}):
                    continue
                defined = (dimension + metric + "按", dimension + metric + "以")
                if not any(line.startswith(defined) for line in re.split(r"[，、；。]", summary)):
                    continue
                for statement in statements:
                    text = statement.removeprefix("各")
                    if text.startswith((series + metric + "按", series + metric + "以")):
                        return True
                    if (metric in {"比例", "占比", "比率"}
                            and re.match(re.escape(series) + r"(?:比例|占比|比率)以.+为分母", text)):
                        return True
        if part.startswith("受限"):
            action = part.removeprefix("受限")
            for clause in clauses:
                if action and clause.endswith(action + subject):
                    qualifier = clause.removesuffix(action + subject)
                    if qualifier.startswith("按") and qualifier.endswith("范围"):
                        bound = qualifier.removeprefix("按").removesuffix("范围")
                        if bound in {"允许", "许可", "获准"} and any(c.startswith("按" + bound + action) for c in conditions):
                            return True
    return False


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
    exact = list(dict.fromkeys(name for text in candidates if (name := _short_heading(text))
                               and all(part in name for part in named)))
    if exact:
        return exact
    purpose = _short_heading(op["purpose"])
    if purpose and all(part in purpose or _scope_in_purpose(part, purpose, op["summary"], claims) for part in named):
        return [purpose]
    return []


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
    nonempty = [names for names in preferred.values() if names]
    if len(set(nonempty)) != len(nonempty):
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
