"""Repeatable prompt evaluation. Default: offline checks; --live explicitly calls configured models.

Only the bundled synthetic cases are sent. Rubrics never enter model inputs.
Real application settings are read-only, usage/output artifacts go to this project.
Semantic correctness always requires rubric review; format tests cannot prove it.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from businessmap.providers import ProviderService, ToolError, provider_destination

DATA = ROOT / "backend/evals/business_analysis"
KEYS = {"overview", "entities", "flows", "rules", "data"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ReadOnlySettings:
    """Provider adapter facade: no vault initialization or writes to user SQLite."""
    def __init__(self, directory):
        self.data_dir = Path(directory).expanduser().resolve()
        self.db_path = self.data_dir / "workspace.sqlite"
        self.local = {}

    def config_get(self, key, default=None):
        if key in self.local:
            return self.local[key]
        if key == "provider_usage" or not self.db_path.is_file():
            return default
        if key not in {"providers", "active_provider_id"}:
            raise ValueError("Evaluation may only read model settings")
        with sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def config_update(self, key, updater, default=None):
        if key != "provider_usage":
            raise ValueError("Evaluation cannot change application settings")
        self.local[key] = updater(self.local.get(key, default))
        return self.local[key]


def validate_response(text, inputs):
    errors, inline_errors = [], []
    try:
        data = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        return {"schema_errors": ["Response is not a single strict JSON value"],
                "runtime_compatible": False, "inline_citation_errors": [], "semantic_review": "not_scored"}
    notes = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(data, dict) or set(data) != {"notes"} or not isinstance(notes, list) or len(notes) > 5:
        return {"schema_errors": ["Expected object with only notes array, length 0–5"],
                "runtime_compatible": False, "inline_citation_errors": [], "semantic_review": "not_scored"}
    files = {f["path"]: len(f["content"].splitlines()) for f in inputs["files"]}
    seen = set()
    generated = {inputs["note_paths"].get(n.get("key")) for n in notes if isinstance(n, dict) and isinstance(n.get("key"), str)}
    for index, note in enumerate(notes):
        label = f"note[{index}]"
        if not isinstance(note, dict) or set(note) != {"key", "title", "content", "sources"}:
            errors.append(label + ": invalid fields")
            continue
        key = note["key"]
        if not isinstance(key, str) or key not in KEYS or key in seen:
            errors.append(label + ": invalid or duplicate key")
        if isinstance(key, str):
            seen.add(key)
        if any(not isinstance(note[k], str) or not note[k].strip() for k in ("title", "content")):
            errors.append(label + ": empty title/content")
        sources = note["sources"]
        if not isinstance(sources, list) or not sources:
            errors.append(label + ": missing sources")
            sources = []
        valid_sources = []
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"path", "line_start", "line_end"}:
                errors.append(label + ": invalid source fields")
                continue
            path, start, end = source["path"], source["line_start"], source["line_end"]
            if (not isinstance(path, str) or path not in files or type(start) is not int or type(end) is not int
                    or not 1 <= start <= end <= files[path]):
                errors.append(label + ": nonexistent source or line range")
            else:
                valid_sources.append(source)
        content = note["content"] if isinstance(note["content"], str) else ""
        citations = re.findall(r"[（(]([^()（）\n]+):L(\d+)-L(\d+)[）)]", content)
        if not citations:
            inline_errors.append(label + ": no inline evidence citation")
        for path, start, end in citations:
            if not any(s["path"] == path and s["line_start"] <= int(start) <= int(end) <= s["line_end"] for s in valid_sources):
                inline_errors.append(label + ": inline citation not covered by valid sources")
        for target in re.findall(r"\[\[([^\]\n]+)\]\]", content):
            target = target.split("|", 1)[0].split("#", 1)[0]
            if not target.endswith(".md"):
                target += ".md"
            if target not in generated or ".." in PurePosixPath(target).parts:
                errors.append(label + ": wiki target was not generated from note_paths")
    return {"schema_errors": errors, "runtime_compatible": not errors and 1 <= len(notes) <= 5,
            "empty_evidence_response": len(notes) == 0, "inline_citation_errors": inline_errors,
            "semantic_review": "pending_rubric_review" if not errors else "not_scored"}


def load_cases():
    cases = read_json(DATA / "cases.json")
    ids = set()
    for case in cases:
        assert case["id"] not in ids
        ids.add(case["id"])
        assert set(case["input"]["note_paths"]) == KEYS
        assert case["input"]["files"] and case["rubric"]["required_facts"]
        for source in case["input"]["files"]:
            assert isinstance(source["content"], str) and source["path"]
        assert "rubric" not in case["input"]
    return cases


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Existing app data directory; read-only access")
    parser.add_argument("--list-providers", action="store_true")
    parser.add_argument("--live", action="store_true", help="Actually call selected configured providers")
    parser.add_argument("--provider-id", action="append", default=[])
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--prompt", choices=["v1", "v2", "v3", "v4", "both"], default="both")
    parser.add_argument("--repeats", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument("--max-calls", type=int, default=16)
    parser.add_argument("--max-output", type=int, default=3500)
    parser.add_argument("--max-seconds", type=int, default=600)
    parser.add_argument("--responses", type=Path, help="Offline import: array of {id,response} from a disclosed evaluation source")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/prompt-eval/report.json")
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 64 or not 256 <= args.max_output <= 6000 or not 1 <= args.max_seconds <= 3600:
        parser.error("max-calls must be 1–64, max-output 256–6000, max-seconds 1–3600")
    if args.live and args.responses:
        parser.error("--live and --responses are mutually exclusive")
    cases = load_cases()
    wanted = set(args.case_id)
    if wanted - {c["id"] for c in cases}:
        parser.error("Unknown case-id")
    cases = [c for c in cases if not wanted or c["id"] in wanted]
    versions = ["v1", "v4"] if args.prompt == "both" else [args.prompt]
    settings = ReadOnlySettings(args.data_dir) if args.data_dir else None
    service = ProviderService(settings) if settings else None
    if args.list_providers:
        if not service:
            parser.error("--list-providers needs --data-dir")
        print(json.dumps([{k: p[k] for k in ("id", "name", "kind", "model", "has_key", "secret_storage")}
                          for p in service.list()], ensure_ascii=False, indent=2))
        return 0
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "cases_sha256": digest(DATA / "cases.json"),
              "prompt_sha256": {v: digest(DATA / f"prompt-{v}.txt") for v in versions}, "case_count": len(cases),
              "mode": "live_provider_api" if args.live else "offline_import" if args.responses else "offline_preflight",
              "provider_call_attempts": 0, "runs": [], "semantic_review": "pending",
              "checks_performed": ["fixture_structure", "prompt_hashes"],
              "limits": {"max_calls": args.max_calls, "max_output_tokens_per_call": args.max_output, "between_call_deadline_seconds": args.max_seconds},
              "limitations": ["Valid JSON and citations do not prove semantic correctness.",
                              "Candidate notes:[] is intentionally safe but current product rejects empty analysis.",
                              "Offline checks/imports are not cross-vendor API tests."]}
    if args.responses:
        responses = read_json(args.responses)
        report["import_source"] = {"path": str(args.responses.resolve()), "sha256": digest(args.responses),
                                   "producer": "externally supplied; runner cannot verify producer identity"}
        report["checks_performed"].extend(["response_schema", "source_coordinates", "inline_citation_coverage", "wiki_targets"])
        if len(versions) != 1 or not isinstance(responses, list):
            parser.error("Offline import needs --prompt v1, v2, v3 or v4 and a response array")
        mapped = {}
        for item in responses:
            if not isinstance(item, dict) or item.get("id") in mapped or item.get("id") not in {c["id"] for c in cases}:
                parser.error("Unknown or duplicate imported case")
            mapped[item["id"]] = item
        for case in cases:
            if case["id"] not in mapped:
                report["runs"].append({"case_id": case["id"], "status": "missing"})
                continue
            value = mapped[case["id"]]["response"]
            raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            report["runs"].append({"case_id": case["id"], "prompt": versions[0], "status": "generated_response_imported",
                                   "checks": validate_response(raw, case["input"])})
    elif args.live:
        report["checks_performed"].extend(["provider_api_calls", "response_schema", "source_coordinates", "inline_citation_coverage", "wiki_targets"])
        if not service or not args.provider_id:
            parser.error("--live needs --data-dir and explicit --provider-id; no auto-selection")
        providers = [service.get(pid) for pid in dict.fromkeys(args.provider_id)]
        if any(p is None for p in providers):
            parser.error("Unknown provider-id")
        planned = len(providers) * len(versions) * len(cases) * args.repeats
        if planned > args.max_calls:
            parser.error(f"Plan needs {planned} calls, exceeding --max-calls {args.max_calls}; reduce cases or explicitly increase budget")
        deadline = time.monotonic() + args.max_seconds
        for provider in providers:
            for version in versions:
                system = (DATA / f"prompt-{version}.txt").read_text(encoding="utf-8").strip()
                for case in cases:
                    for repetition in range(args.repeats):
                        entry = {"provider_id": provider["id"], "model": provider["model"], "kind": provider["kind"],
                                 "prompt": version, "case_id": case["id"], "repetition": repetition + 1}
                        started = time.monotonic()
                        if started >= deadline:
                            report["stopped"] = "between_call_deadline_reached"
                            save_report(args.output, report)
                            return 1
                        # Only input is sent. The answer rubric is never included.
                        report["provider_call_attempts"] += 1
                        try:
                            response = service.complete(system, json.dumps(case["input"], ensure_ascii=False),
                                provider_id=provider["id"], max_output=args.max_output,
                                expected_destination=provider_destination(provider))
                            entry.update(status="response_received", output=response["text"], usage=response["usage"],
                                         checks=validate_response(response["text"], case["input"]))
                        except ToolError as exc:
                            entry.update(status="error", error=str(exc))
                        entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
                        report["runs"].append(entry)
                        save_report(args.output, report)
                        print(f"{provider['name']} / {version} / {case['id']}: {entry['status']}", flush=True)
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    save_report(args.output, report)
    print(json.dumps({"report": str(args.output.resolve()), "mode": report["mode"],
                      "case_count": len(cases), "provider_call_attempts": report["provider_call_attempts"]}, ensure_ascii=False))
    return 1 if any(r["status"] in {"error", "missing"} or r.get("checks", {}).get("schema_errors")
                   or r.get("checks", {}).get("inline_citation_errors") for r in report["runs"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
