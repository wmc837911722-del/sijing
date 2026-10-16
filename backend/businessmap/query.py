"""Shared deterministic read-only query boundary for HTTP, chat and MCP."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .providers import Secrets, ToolError, config_update


MAX_ROWS = 1000
TIMEOUT_SECONDS = 5
MAX_RESULT_BYTES = 2_000_000
# Unknown functions are denied: a SELECT can call a function with side effects.
SAFE_FUNCTIONS = {
    "ABS", "AVG", "COUNT", "SUM", "MIN", "MAX", "ROUND", "CEIL", "CEILING", "FLOOR",
    "COALESCE", "IFNULL", "NULLIF", "IF", "IIF", "CASE", "CAST", "TRY_CAST", "CONVERT",
    "LOWER", "UPPER", "LENGTH", "CHAR_LENGTH", "CHARACTER_LENGTH", "SUBSTRING", "SUBSTR",
    "TRIM", "LTRIM", "RTRIM", "REPLACE", "CONCAT", "CONCAT_WS", "LEFT", "RIGHT",
    "DATE", "DATETIME", "TIME", "STRFTIME", "JULIANDAY", "UNIXEPOCH", "DATE_TRUNC",
    "TIMESTAMP_TRUNC", "TIME_TO_STR", "STR_TO_TIME", "STR_TO_DATE", "TS_OR_DS_TO_DATE",
    "DATE_FORMAT", "DATE_ADD", "DATE_SUB", "DATEDIFF", "DATE_DIFF", "TIMESTAMPDIFF",
    "YEAR", "MONTH", "DAY", "DAYOFMONTH", "EXTRACT", "CURRENT_DATE", "CURRENT_TIME",
    "CURRENT_TIMESTAMP", "NOW", "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD",
    "FIRST_VALUE", "LAST_VALUE", "NTILE", "PERCENT_RANK", "CUME_DIST", "NTH_VALUE",
    "BOOL_AND", "BOOL_OR", "EVERY", "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE",
    "VAR_POP", "VAR_SAMP", "JSON_EXTRACT", "JSON_EXTRACT_SCALAR", "JSON_ARRAY_LENGTH",
    "JSON_TYPE", "JSON_VALID", "GREATEST", "LEAST", "MOD", "POWER", "SQRT", "SIGN",
    "GROUP_CONCAT", "STRING_AGG", "ARRAY_AGG", "FILTER", "ISNULL", "TO_CHAR",
    "TO_DATE", "TO_TIMESTAMP", "DATE_PART", "UNIX_TIMESTAMP", "FROM_UNIXTIME",
}
SQLITE_FUNCTIONS = {name.lower() for name in SAFE_FUNCTIONS} | {"like", "glob"}
ANONYMOUS_FUNCTIONS = {
    "sqlite": {"DATETIME", "STRFTIME", "JULIANDAY", "UNIXEPOCH", "JSON_TYPE", "JSON_VALID", "JSON_ARRAY_LENGTH"},
    "postgres": {"DATE_PART", "TO_CHAR", "TO_DATE", "TO_TIMESTAMP", "STRING_AGG", "BOOL_AND", "BOOL_OR"},
    "mysql": {"DATE_FORMAT", "TIMESTAMPDIFF", "UNIX_TIMESTAMP", "FROM_UNIXTIME", "JSON_EXTRACT", "JSON_TYPE", "JSON_VALID", "JSON_ARRAY_LENGTH"},
}
FORBIDDEN_NODES = {
    "Insert", "Update", "Delete", "Create", "Drop", "Alter", "Command", "Merge",
    "Copy", "Attach", "Detach", "Pragma", "Transaction", "Commit", "Rollback",
    "Grant", "Revoke", "Into", "Lock", "Set", "Use", "Execute", "Analyze",
    "Vacuum", "Hint", "PropertyEQ", "SessionParameter", "Parameter", "Placeholder",
    "Pivot", "Unpivot", "UDTF", "TableFromRows", "UserDefinedFunction",
}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def _identifier(identifier, dialect):
    if identifier is None:
        return ""
    value = identifier.name
    return value if dialect == "postgres" and identifier.args.get("quoted") else value.lower()


def validate_sql(sql, dialect, allowed_tables, database=None):
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 20000:
        raise ToolError("SQL 必须是 20,000 字符以内的非空只读查询。")
    if "/*+" in sql or "\x00" in sql:
        raise ToolError("不允许 SQL 执行提示或控制字符。")
    try:
        statements = sqlglot.parse(sql, read=dialect)
        if len(statements) != 1 or statements[0] is None:
            raise ToolError("只允许一条 SELECT 或 WITH 查询。")
        tree = statements[0]
        if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise ToolError("只允许 SELECT 或 WITH 查询，禁止写入与管理语句。")
        for node in tree.walk():
            if type(node).__name__ in FORBIDDEN_NODES:
                raise ToolError(f"查询包含禁止的 SQL 操作：{type(node).__name__}。")
            if isinstance(node, exp.Func):
                name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
                if name not in SAFE_FUNCTIONS or (isinstance(node, exp.Anonymous) and name not in ANONYMOUS_FUNCTIONS.get(dialect, set())):
                    raise ToolError(f"不允许调用未列入安全清单的函数：{name}。")
                if isinstance(node.parent, exp.Dot):
                    raise ToolError("不允许限定命名空间的函数调用。")
        canonical = set(allowed_tables)
        if dialect != "postgres":
            canonical = {name.lower() for name in canonical}
        for scope in traverse_scope(tree):
            for _, (_, source) in scope.selected_sources.items():
                if not isinstance(source, exp.Table):
                    continue  # A CTE/subquery scope is checked independently.
                if not isinstance(source.this, exp.Identifier):
                    raise ToolError("不允许表函数或动态数据源。")
                name = _identifier(source.this, dialect)
                schema = _identifier(source.args.get("db"), dialect)
                catalog = _identifier(source.args.get("catalog"), dialect)
                if catalog:
                    raise ToolError("不允许跨数据库查询。")
                if dialect == "postgres":
                    target = f"{schema or 'public'}.{name}"
                    permitted = target in canonical or (not schema and name in canonical)
                else:
                    expected = "main" if dialect == "sqlite" else (database or "").lower()
                    if schema and schema != expected:
                        raise ToolError("不允许访问其他数据库或附加数据库。")
                    permitted = name in canonical or f"{schema}.{name}" in canonical
                if not permitted:
                    raise ToolError(f"表不在允许范围或不存在：{source.sql(dialect=dialect)}。", 403)
                if dialect == "postgres" and not schema:
                    source.set("db", exp.to_identifier("public"))
        return tree.sql(dialect=dialect)
    except ToolError:
        raise
    except Exception:
        raise ToolError("SQL 无法安全解析，未执行。") from None


def _cell(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, Decimal):
        return str(value)  # Preserve monetary precision rather than silently round.
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return "base64:" + base64.b64encode(value).decode()
    return str(value)


class QueryService:
    def __init__(self, store):
        self.store = store
        self.secrets = Secrets(store.data_dir)

    def list(self):
        items = self.store.config_get("datasources", [])
        demo = Path(self.store.data_dir) / "demo.sqlite"
        if demo.is_file() and not any(d["id"] == "demo" for d in items):
            items = [{"id": "demo", "name": "演示数据库 · 示例订单", "kind": "sqlite", "path": str(demo),
                      "status": "ready", "allowed_tables": ["orders", "refunds"], "demo": True}] + items
        return [{**d, "has_password": bool(self.secrets.get(f"datasource:{d['id']}")),
                 "secret_storage": self.secrets.storage(f"datasource:{d['id']}")} for d in items]

    def get(self, datasource_id):
        item = next((d for d in self.list() if d["id"] == datasource_id), None)
        if not item:
            raise ToolError("数据源不存在。", 404)
        return item

    def save(self, data):
        kind = data.get("kind")
        if kind not in ("sqlite", "postgresql", "mysql"):
            raise ToolError("仅支持 SQLite、PostgreSQL 和 MySQL。")
        name = str(data.get("name", "")).strip()
        if not name or len(name) > 160:
            raise ToolError("请填写数据源名称。")
        item = {"id": uuid.uuid4().hex, "name": name, "kind": kind, "status": "untested", "demo": False}
        if kind == "sqlite":
            path = Path(str(data.get("path", ""))).expanduser().resolve()
            if not path.is_file():
                raise ToolError("SQLite 文件不存在；只读连接不会创建数据库。")
            item["path"] = str(path)
        else:
            for field in ("host", "database", "username"):
                value = str(data.get(field, "")).strip()
                if not value or len(value) > 255 or "\x00" in value:
                    raise ToolError("请填写数据库主机、库名和只读用户名。")
                item[field] = value
            try:
                item["port"] = int(data.get("port") or (5432 if kind == "postgresql" else 3306))
            except (ValueError, TypeError):
                raise ToolError("数据库端口无效。") from None
            if not 1 <= item["port"] <= 65535:
                raise ToolError("数据库端口无效。")
            if data.get("password") is not None:
                self.secrets.set(f"datasource:{item['id']}", data["password"])
        allowed = data.get("allowed_tables")
        if allowed is not None:
            if not isinstance(allowed, list) or len(allowed) > 1000 or any(not isinstance(n, str) or not n.strip() or len(n) > 300 for n in allowed):
                raise ToolError("allowed_tables 必须是表名数组。")
            item["allowed_tables"] = list(dict.fromkeys(allowed))
        config_update(self.store, "datasources", lambda old: (old or []) + [item], [])
        return self.get(item["id"])

    @contextmanager
    def connection(self, ds):
        conn = None
        try:
            if ds["kind"] == "sqlite":
                path = Path(ds["path"]).resolve()
                if not path.is_file():
                    raise ToolError("SQLite 文件已不存在。")
                conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=TIMEOUT_SECONDS)
                conn.execute("PRAGMA query_only = ON")
                conn.enable_load_extension(False)
                if hasattr(conn, "setlimit"):
                    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_RESULT_BYTES)
                    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 25000)
                    conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
                deadline = time.monotonic() + TIMEOUT_SECONDS
                conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            elif ds["kind"] == "postgresql":
                import psycopg
                conn = psycopg.connect(host=ds["host"], port=ds["port"], dbname=ds["database"], user=ds["username"],
                                       password=self.secrets.get(f"datasource:{ds['id']}") or "", connect_timeout=5,
                                       options=f"-c default_transaction_read_only=on -c statement_timeout={TIMEOUT_SECONDS * 1000} -c lock_timeout=1000")
                conn.execute("BEGIN READ ONLY")
                # All physical tables are qualified during validation. Restrict function
                # resolution to built-ins so a public-schema overload cannot be invoked.
                conn.execute("SET LOCAL search_path TO pg_catalog")
                flags = conn.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
                can_write = conn.execute("SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','v','m') AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))").fetchone()[0]
                can_create = conn.execute("SELECT has_database_privilege(current_user,current_database(),'CREATE') OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema' AND has_schema_privilege(current_user,oid,'CREATE'))").fetchone()[0]
                if not flags or any(flags) or can_write or can_create:
                    raise ToolError("此 PostgreSQL 账号具有写入、创建或特权权限。请使用专用只读账号。", 403)
            else:
                import pymysql
                conn = pymysql.connect(host=ds["host"], port=ds["port"], database=ds["database"], user=ds["username"],
                                       password=self.secrets.get(f"datasource:{ds['id']}") or "", connect_timeout=5,
                                       read_timeout=TIMEOUT_SECONDS + 1, write_timeout=TIMEOUT_SECONDS + 1,
                                       autocommit=False, local_infile=False, charset="utf8mb4")
                with conn.cursor() as cursor:
                    cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
                    for grant_row in cursor.fetchall():
                        grant = grant_row[0]
                        match = re.match(r"GRANT (.+?) ON ", grant, re.I)
                        if not match or "WITH GRANT OPTION" in grant.upper():
                            raise ToolError("无法证明 MySQL 角色为只读。请使用直接授予 SELECT 的专用账号。", 403)
                        privileges = {p.strip().upper() for p in match.group(1).split(",")}
                        if not privileges <= {"SELECT", "SHOW VIEW", "USAGE"}:
                            raise ToolError("此 MySQL 账号具有非只读权限。请使用专用只读账号。", 403)
                    if "mariadb" in conn.get_server_info().lower():
                        cursor.execute(f"SET SESSION max_statement_time = {TIMEOUT_SECONDS}")
                    else:
                        cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {TIMEOUT_SECONDS * 1000}")
                    cursor.execute("START TRANSACTION READ ONLY")
            yield conn
        except ToolError:
            raise
        except Exception as exc:
            code = getattr(exc, "sqlstate", None)
            if not code and getattr(exc, "args", None) and isinstance(exc.args[0], int):
                code = str(exc.args[0])
            if code in ("57014", "3024", "1969") or (isinstance(exc, sqlite3.OperationalError) and "interrupted" in str(exc)):
                raise ToolError("查询超过 5 秒时间限制，已取消。", 408) from None
            raise ToolError(f"数据库操作失败（{type(exc).__name__}{':' + code if code else ''}）。请检查连接、只读权限和 SQL。", 400) from None
        finally:
            if conn is not None:
                try:
                    conn.rollback()
                    conn.close()
                except Exception:
                    pass

    def _schema(self, conn, ds):
        tables = []
        if ds["kind"] == "sqlite":
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND upper(sql) NOT LIKE '%CREATE VIRTUAL TABLE%' ORDER BY name").fetchall()
            for (name,) in rows[:1000]:
                escaped = name.replace('"', '""')
                columns = conn.execute(f'PRAGMA table_info("{escaped}")').fetchall()
                tables.append({"name": name, "columns": [{"name": c[1], "type": c[2]} for c in columns]})
        else:
            cursor = conn.cursor()
            try:
                if ds["kind"] == "postgresql":
                    cursor.execute("SELECT c.table_schema,c.table_name,c.column_name,c.data_type FROM information_schema.columns c JOIN information_schema.tables t ON c.table_schema=t.table_schema AND c.table_name=t.table_name WHERE t.table_type='BASE TABLE' AND c.table_schema NOT IN ('pg_catalog','information_schema') ORDER BY c.table_schema,c.table_name,c.ordinal_position LIMIT 20000")
                else:
                    cursor.execute("SELECT c.table_schema,c.table_name,c.column_name,c.data_type FROM information_schema.columns c JOIN information_schema.tables t ON c.table_schema=t.table_schema AND c.table_name=t.table_name WHERE t.table_type='BASE TABLE' AND c.table_schema=DATABASE() ORDER BY c.table_name,c.ordinal_position LIMIT 20000")
                found = {}
                for schema, table, col, kind in cursor.fetchall():
                    name = f"{schema}.{table}" if ds["kind"] == "postgresql" else table
                    found.setdefault(name, []).append({"name": col, "type": kind})
                tables = [{"name": k, "columns": v} for k, v in found.items()]
            finally:
                cursor.close()
        allowed = ds.get("allowed_tables")
        if allowed is not None:
            def permitted(name):
                if ds["kind"] == "postgresql":
                    return name in allowed or (name.startswith("public.") and name[7:] in allowed)
                return name.lower() in {s.lower() for s in allowed}
            tables = [t for t in tables if permitted(t["name"])]
        return {"tables": tables}

    def schema(self, datasource_id):
        ds = self.get(datasource_id)
        with self.connection(ds) as conn:
            return self._schema(conn, ds)

    def test(self, datasource_id):
        schema = self.schema(datasource_id)
        config_update(self.store, "datasources", lambda old: [{**d, "status": "ready"} if d["id"] == datasource_id else d for d in (old or [])], [])
        return {"ok": True, "message": f"真实连接成功，只读保护已启用；允许访问 {len(schema['tables'])} 张表。"}

    def execute(self, datasource_id, sql):
        ds = self.get(datasource_id)
        dialect = {"sqlite": "sqlite", "postgresql": "postgres", "mysql": "mysql"}[ds["kind"]]
        started = time.monotonic()
        with self.connection(ds) as conn:
            schema = self._schema(conn, ds)
            allowed = [t["name"] for t in schema["tables"]]
            normalized = validate_sql(sql, dialect, allowed, ds.get("database"))
            if ds["kind"] == "sqlite":
                tables = {name.lower() for name in allowed}
                def authorizer(action, arg1, arg2, db, source):
                    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                        return sqlite3.SQLITE_OK
                    if action == sqlite3.SQLITE_READ:
                        # SQLite emits (table, '', db=None) for COUNT(*) because no
                        # particular column is read. SQL scope was already validated.
                        main_read = db == "main" or (db is None and arg2 == "")
                        return sqlite3.SQLITE_OK if (arg1 or "").lower() in tables and main_read else sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_FUNCTION:
                        return sqlite3.SQLITE_OK if (arg2 or "").lower() in SQLITE_FUNCTIONS else sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_DENY
                conn.set_authorizer(authorizer)
            cursor = conn.cursor()
            try:
                cursor.execute(f"SELECT * FROM ({normalized}) AS businessmap_result LIMIT {MAX_ROWS + 1}")
                columns = [c[0] for c in cursor.description or []]
                fetched = cursor.fetchmany(MAX_ROWS + 1)
                truncated = len(fetched) > MAX_ROWS
                rows, size = [], 0
                for row in fetched[:MAX_ROWS]:
                    encoded = [_cell(v) for v in row]
                    size += len(json.dumps(encoded, ensure_ascii=False).encode())
                    if size > MAX_RESULT_BYTES:
                        truncated = True
                        break
                    rows.append(encoded)
            finally:
                cursor.close()
        result = {"id": uuid.uuid4().hex, "sql": normalized, "columns": columns, "rows": rows,
                  "row_count": len(rows), "truncated": truncated, "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                  "executed_at": utcnow(), "datasource_id": datasource_id}
        self.store.config_set(f"query_result:{result['id']}", result)
        self.store.activity("只读查询完成", f"{ds['name']} · 返回 {len(rows)} 行" + ("（已截断）" if truncated else ""), "query")
        return result

    def reports(self):
        return self.store.config_get("reports", [])

    def create_report(self, result_id, title=None, chart_type="table"):
        result = self.store.config_get(f"query_result:{result_id}")
        if not result:
            raise ToolError("查询结果不存在，请先执行真实查询。", 404)
        if chart_type not in ("bar", "line", "table"):
            raise ToolError("不支持的报表类型。")
        report = {"id": uuid.uuid4().hex, "title": (title or "查询报表")[:160], "chart_type": chart_type,
                  "result": result, "created_at": utcnow()}
        config_update(self.store, "reports", lambda old: [report] + (old or [])[:99], [])
        self.store.activity("保存报表", report["title"], "report")
        return report
