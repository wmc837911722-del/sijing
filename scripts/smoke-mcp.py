"""Exercise a frozen Sijing service through real MCP stdio transport.

Run with the project virtualenv after packaging. The server runs in a fresh
project-local directory; no provider credentials or external databases are used.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = {
    "knowledge_search", "knowledge_read", "knowledge_graph", "datasources",
    "datasource_schema", "query_readonly",
    "knowledge_list", "knowledge_outline", "knowledge_links",
}


def default_executable():
    if sys.platform == "win32":
        return ROOT / "release" / "win-unpacked" / "resources" / "backend" / "businessmap-service.exe"
    if sys.platform == "darwin":
        metadata = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        build = metadata["build"]
        app_name = (build.get("mac", {}).get("executableName") or build.get("executableName")
                    or build.get("productName") or metadata.get("productName") or metadata["name"])
        candidates = sorted((ROOT / "release").glob(f"mac*/{app_name}.app/Contents/Resources/backend/businessmap-service"))
        return next((path for path in candidates if path.is_file()),
                    ROOT / "release" / "mac" / f"{app_name}.app" / "Contents" / "Resources" / "backend" / "businessmap-service")
    return ROOT / "release" / "linux-unpacked" / "resources" / "backend" / "businessmap-service"


def check(condition, explanation):
    if not condition:
        raise AssertionError(explanation)


async def call(session, tool, arguments):
    result = await session.call_tool(tool, arguments)
    check(not result.isError, f"{tool} failed: {result.content}")
    if result.structuredContent is not None:
        return result.structuredContent
    for block in result.content:
        if block.type == "text":
            return json.loads(block.text)
    raise AssertionError(f"{tool} returned no structured data")


async def smoke(executable, data_dir, report):
    data_dir.mkdir(parents=True, exist_ok=False)
    temporary = data_dir / "temp"
    temporary.mkdir()
    params = StdioServerParameters(
        command=str(executable), args=["--mcp", "--data-dir", str(data_dir)],
        cwd=ROOT,
        env={"PYTHONUTF8": "1", "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
             "PYTHONPYCACHEPREFIX": str(ROOT / ".cache" / "pycache")},
    )
    with (data_dir / "server.stderr.log").open("w", encoding="utf-8") as errors:
        async with stdio_client(params, errlog=errors) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as session:
                initialized = await session.initialize()
                check(initialized.serverInfo.name == "Sijing", "unexpected MCP server identity")
                report["server"] = initialized.serverInfo.model_dump()
                report["protocol_version"] = initialized.protocolVersion
                listed = await session.list_tools()
                check({tool.name for tool in listed.tools} == EXPECTED_TOOLS, "unexpected MCP tools")
                check(all(tool.annotations and tool.annotations.readOnlyHint for tool in listed.tools),
                      "all published tools must declare read-only behavior")
                report["tools"] = sorted(tool.name for tool in listed.tools)

                found = await call(session, "knowledge_search", {"query": "退款"})
                check(found["notes"], "demo knowledge was not searchable")
                note = await call(session, "knowledge_read", {"path": found["notes"][0]["path"]})
                check(note["content"] and note["revision"] and note["is_current"], "knowledge read missing evidence")
                outline = await call(session, "knowledge_outline", {"path": note["path"]})
                check(outline["headings"] and outline["revision"] == note["revision"], "outline missing current headings")
                section = await call(session, "knowledge_read", {
                    "path": note["path"], "heading": outline["headings"][0]["heading"], "revision": outline["revision"],
                })
                check(section["content"] and section["revision"] == note["revision"], "section read missing evidence")
                linked = await call(session, "knowledge_links", {"path": note["path"]})
                check(linked["edges"] and linked["nodes"], "progressive navigation missing relationships")
                listed_notes = await call(session, "knowledge_list", {"limit": 2})
                check(len(listed_notes["notes"]) == 2 and listed_notes["next_offset"] == 2, "note listing pagination missing")
                graph = await call(session, "knowledge_graph", {})
                check(len(graph["nodes"]) >= 6 and graph["edges"], "linked demo knowledge graph missing")
                report["knowledge"] = {"search_hits": len(found["notes"]), "nodes": len(graph["nodes"]),
                                       "edges": len(graph["edges"]), "read_path": note["path"]}

                sources = await call(session, "datasources", {})
                demo = next(item for item in sources["datasources"] if item.get("demo"))
                schema = await call(session, "datasource_schema", {"datasource_id": demo["id"]})
                check({table["name"] for table in schema["tables"]} == {"orders", "refunds"}, "unexpected demo schema")
                database = data_dir / "demo.sqlite"
                digest_before = hashlib.sha256(database.read_bytes()).hexdigest()
                count = await call(session, "query_readonly", {
                    "datasource_id": demo["id"], "sql": "SELECT COUNT(*) AS order_count FROM orders",
                })
                check(count["rows"] == [[8]], "COUNT(*) returned unexpected demo data")
                refunds = await call(session, "query_readonly", {
                    "datasource_id": demo["id"],
                    "sql": "SELECT SUM(amount) AS total FROM refunds WHERE status = 'completed'",
                })
                check(refunds["rows"] == [[190.0]], "completed refund sum is incorrect")
                report["query"] = {"orders": count["rows"][0][0], "completed_refunds": refunds["rows"][0][0]}

                rejected = []
                for sql in (
                    "DELETE FROM orders",
                    "UPDATE orders SET amount = 0",
                    "DROP TABLE refunds",
                    "SELECT * FROM orders; DELETE FROM orders",
                    "PRAGMA writable_schema = ON",
                    "SELECT * FROM sqlite_master",
                ):
                    result = await session.call_tool("query_readonly", {"datasource_id": demo["id"], "sql": sql})
                    check(result.isError, f"unsafe query unexpectedly succeeded: {sql}")
                    rejected.append(sql)
                after = await call(session, "query_readonly", {
                    "datasource_id": demo["id"], "sql": "SELECT COUNT(*) AS order_count FROM orders",
                })
                check(after["rows"] == [[8]], "denied writes changed database rows")
                digest_after = hashlib.sha256(database.read_bytes()).hexdigest()
                check(digest_before == digest_after, "demo database bytes changed")
                report["rejected_queries"] = rejected
                report["database_unchanged"] = True
                escaped = await session.call_tool("knowledge_read", {"path": "../demo.sqlite"})
                check(escaped.isError, "knowledge read escaped the vault")
                report["vault_traversal_rejected"] = True
    stderr_path = data_dir / "server.stderr.log"
    report["server_stderr"] = str(stderr_path)
    report["server_stderr_has_traceback"] = "Traceback (most recent call last)" in stderr_path.read_text(encoding="utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, default=default_executable())
    parser.add_argument("--data-dir", type=Path,
                        default=ROOT / ".tmp" / ("packaged-mcp-" + uuid.uuid4().hex[:12]))
    args = parser.parse_args()
    executable, data_dir = args.executable.resolve(), args.data_dir.resolve()
    check(executable.is_file(), f"packaged service not found: {executable}")
    report = {"status": "running", "executable": str(executable), "data_dir": str(data_dir),
              "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
              "tested_at": datetime.now(timezone.utc).isoformat()}
    try:
        asyncio.run(smoke(executable, data_dir, report))
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = repr(exc)
        raise
    finally:
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        (artifacts / "packaged-mcp-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
