#!/usr/bin/env python3
"""Pharmacovigilance case intake service using only the Python standard library."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PORT = 8201
ROLES = {"reporter", "regional_lead", "medical_reviewer", "global_admin"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    current = value or utcnow()
    return current.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None, default: datetime | None = None) -> datetime:
    if not value:
        if default is None:
            raise ApiError(400, "missing_time", "必须提供 ISO 8601 时间")
        return default
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def report_deadline(received_at: datetime, serious: bool, fatal: bool) -> datetime:
    if serious:
        return received_at + timedelta(days=7 if fatal else 15)
    return received_at + timedelta(days=90)


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()
        self.migrate_schema()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def migrate_schema(self) -> None:
        """Bring databases created by older versions in line with origin tracking."""
        with self.tx() as conn:
            case_cols = self._columns("cases")
            if "merged_at" not in case_cols:
                conn.execute("ALTER TABLE cases ADD COLUMN merged_at TEXT")
            for table in ("intakes", "followups", "reports", "medical_reviews"):
                cols = self._columns(table)
                if "origin_case_id" not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN origin_case_id INTEGER REFERENCES cases(id)")
                    conn.execute(f"UPDATE {table} SET origin_case_id=case_id WHERE origin_case_id IS NULL")
                if "origin_case_no" not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN origin_case_no TEXT")
                    conn.execute(
                        f"UPDATE {table} SET origin_case_no=(SELECT case_no FROM cases WHERE cases.id={table}.origin_case_id) "
                        "WHERE origin_case_no IS NULL"
                    )

        def rebuild(conn: sqlite3.Connection, table: str, create_sql: str, columns: str) -> None:
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            conn.execute(create_sql)
            conn.execute(
                f"INSERT INTO {table}({columns}) SELECT {columns} FROM {table}_old"
            )
            conn.execute(f"DROP TABLE {table}_old")

        followup_cols = self._columns("followups")
        review_cols = self._columns("medical_reviews")
        needs_followup_rebuild = "origin_case_id" not in followup_cols
        needs_review_rebuild = "origin_case_id" not in review_cols
        if needs_followup_rebuild or needs_review_rebuild:
            with self.tx() as conn:
                if needs_followup_rebuild:
                    rebuild(
                        conn,
                        "followups",
                        """CREATE TABLE followups (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            case_id INTEGER NOT NULL REFERENCES cases(id),
                            origin_case_id INTEGER NOT NULL REFERENCES cases(id),
                            origin_case_no TEXT,
                            content TEXT NOT NULL, source TEXT NOT NULL,
                            received_at TEXT NOT NULL, revision INTEGER NOT NULL,
                            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                            UNIQUE(origin_case_id, revision)
                        )""",
                        "id,case_id,origin_case_id,origin_case_no,content,source,received_at,revision,created_by,created_at",
                    )
                if needs_review_rebuild:
                    rebuild(
                        conn,
                        "medical_reviews",
                        """CREATE TABLE medical_reviews (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            case_id INTEGER NOT NULL REFERENCES cases(id),
                            origin_case_id INTEGER NOT NULL REFERENCES cases(id),
                            origin_case_no TEXT,
                            case_revision INTEGER NOT NULL,
                            serious INTEGER NOT NULL, fatal INTEGER NOT NULL,
                            causality TEXT NOT NULL, rationale TEXT NOT NULL,
                            reviewer TEXT NOT NULL, created_at TEXT NOT NULL,
                            UNIQUE(origin_case_id, case_revision)
                        )""",
                        "id,case_id,origin_case_id,origin_case_no,case_revision,serious,fatal,causality,rationale,reviewer,created_at",
                    )

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_no TEXT NOT NULL UNIQUE,
                patient_ref TEXT NOT NULL,
                region TEXT NOT NULL,
                product TEXT NOT NULL,
                event_term TEXT NOT NULL,
                onset_at TEXT,
                received_at TEXT NOT NULL,
                serious INTEGER NOT NULL DEFAULT 0,
                fatal INTEGER NOT NULL DEFAULT 0,
                causality TEXT,
                report_due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                revision INTEGER NOT NULL DEFAULT 1,
                merged_into INTEGER REFERENCES cases(id),
                merged_at TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intakes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER REFERENCES cases(id),
                origin_case_id INTEGER,
                source TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS followups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id),
                origin_case_id INTEGER NOT NULL REFERENCES cases(id),
                origin_case_no TEXT,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                received_at TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(origin_case_id, revision)
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id),
                origin_case_id INTEGER NOT NULL REFERENCES cases(id),
                origin_case_no TEXT,
                country TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                submitted_at TEXT,
                submitted_by TEXT,
                late INTEGER NOT NULL DEFAULT 0,
                UNIQUE(case_id, country)
            );
            CREATE TABLE IF NOT EXISTS medical_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id),
                origin_case_id INTEGER NOT NULL REFERENCES cases(id),
                origin_case_no TEXT,
                case_revision INTEGER NOT NULL,
                serious INTEGER NOT NULL,
                fatal INTEGER NOT NULL,
                causality TEXT NOT NULL,
                rationale TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(origin_case_id, case_revision)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER,
                actor TEXT NOT NULL,
                role TEXT NOT NULL,
                action TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    @staticmethod
    def audit(conn: sqlite3.Connection, case_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(case_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (case_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()),
        )

    @staticmethod
    def row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None


class PharmacovigilanceService:
    def __init__(self, db_path: str | Path):
        self.repo = Repository(db_path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor = headers.get("X-User-Id", "").strip()
        role = headers.get("X-Role", "").strip()
        region = headers.get("X-Region", "").strip()
        if not actor or role not in ROLES:
            raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效的 X-Role")
        if role in {"reporter", "regional_lead"} and not region:
            raise ApiError(401, "region_required", "该角色必须提供 X-Region")
        return actor, role, region

    @staticmethod
    def can_access(case: dict[str, Any], role: str, region: str) -> bool:
        return role in {"medical_reviewer", "global_admin"} or case["region"] == region

    def _case(self, conn: sqlite3.Connection, case_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise ApiError(404, "case_not_found", "案例不存在")
        return row

    @staticmethod
    def _merge_root(conn: sqlite3.Connection, case_row: sqlite3.Row) -> sqlite3.Row:
        """Follow the merge chain to the surviving target case."""
        row = case_row
        seen: set[int] = set()
        while row["status"] == "merged" and row["merged_into"] and row["id"] not in seen:
            seen.add(row["id"])
            row = conn.execute("SELECT * FROM cases WHERE id=?", (row["merged_into"],)).fetchone()
        return row

    def create_case(self, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        required = ("patient_ref", "region", "product", "event_term", "source", "dedupe_key")
        missing = [key for key in required if not str(body.get(key, "")).strip()]
        if missing:
            raise ApiError(400, "missing_fields", f"缺少字段: {', '.join(missing)}")
        if role == "reporter" and body["region"] != region:
            raise ApiError(403, "region_forbidden", "只能录入本区域案例")
        if role == "medical_reviewer" and body["region"] not in {"", region}:
            raise ApiError(403, "reviewer_region_forbidden", "医学审核员不能代表区域录入案例")
        received = parse_time(body.get("received_at"), utcnow())
        serious = bool(body.get("serious", False))
        fatal = bool(body.get("fatal", False))
        due = report_deadline(received, serious, fatal)
        now = iso()
        with self.repo.tx() as conn:
            duplicate = conn.execute("SELECT * FROM intakes WHERE dedupe_key=?", (body["dedupe_key"],)).fetchone()
            if duplicate:
                case = self._case(conn, duplicate["case_id"])
                Repository.audit(conn, case["id"], actor, role, "intake_deduplicated", {"dedupe_key": body["dedupe_key"], "source": body["source"]})
                return {"deduplicated": True, "case": dict(case), "intake_id": duplicate["id"]}
            count = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] + 1
            case_no = body.get("case_no") or f"PV-{received.year}-{count:06d}"
            try:
                cursor = conn.execute(
                    """INSERT INTO cases(case_no,patient_ref,region,product,event_term,onset_at,received_at,
                       serious,fatal,causality,report_due_at,status,revision,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (case_no, body["patient_ref"], body["region"], body["product"], body["event_term"],
                     body.get("onset_at"), iso(received), int(serious), int(fatal), body.get("causality"),
                     iso(due), "open", 1, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "case_number_conflict", "案例编号已存在") from exc
            case_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO intakes(case_id,origin_case_id,origin_case_no,source,dedupe_key,payload_json,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (case_id, case_id, case_no, body["source"], body["dedupe_key"], json.dumps(body, ensure_ascii=False, sort_keys=True), iso(received), actor, now),
            )
            Repository.audit(conn, case_id, actor, role, "case_created", {"case_no": case_no, "source": body["source"]})
            case = self._case(conn, case_id)
            return {"deduplicated": False, "case": dict(case)}

    def get_case(self, case_id: int, role: str, region: str) -> dict[str, Any]:
        case_row = self._case(self.repo.conn, case_id)
        if not self.can_access(case_row, role, region):
            raise ApiError(403, "case_forbidden", "无权查看该区域案例")
        conn = self.repo.conn

        # 合并来源（含链式合并时的间接来源），按合并时间排列。
        merge_sources = [
            {"id": r["id"], "case_no": r["case_no"], "product": r["product"],
             "region": r["region"], "merged_at": r["merged_at"], "revision": r["revision"],
             "merged_into": r["merged_into"], "via_case_no": r["via_case_no"],
             "indirect": bool(r["merged_into"] != case_id)}
            for r in conn.execute(
                """WITH RECURSIVE src(id, parent) AS (
                       SELECT id, merged_into FROM cases WHERE merged_into=? AND status='merged'
                       UNION ALL
                       SELECT c.id, c.merged_into FROM cases c JOIN src s ON c.merged_into=s.id AND c.status='merged'
                   )
                   SELECT c.id,c.case_no,c.product,c.region,c.merged_at,c.revision,c.merged_into,
                          (SELECT case_no FROM cases WHERE id=src.parent) AS via_case_no
                   FROM src JOIN cases c ON c.id=src.id ORDER BY c.merged_at,c.id""",
                (case_id,),
            )
        ]
        merged_target = None
        if case_row["status"] == "merged" and case_row["merged_into"]:
            tgt = self._merge_root(conn, case_row)
            merged_target = {"id": tgt["id"], "case_no": tgt["case_no"]}

        privileged = role in {"medical_reviewer", "global_admin"}
        source_ids = [case_id] + [s["id"] for s in merge_sources]
        placeholders = ",".join("?" for _ in source_ids)
        audit_rows = [
            dict(r) for r in conn.execute(
                f"SELECT actor,role,action,detail_json,created_at,case_id FROM audit_log "
                f"WHERE case_id IN ({placeholders}) ORDER BY id",
                source_ids,
            )
        ] if privileged else []

        result = {
            "case": dict(case_row),
            "merged_target": merged_target,
            "merge_sources": merge_sources,
            "intakes": [dict(r) for r in conn.execute("SELECT * FROM intakes WHERE case_id=? ORDER BY id", (case_id,))],
            "followups": [dict(r) for r in conn.execute("SELECT * FROM followups WHERE case_id=? ORDER BY id", (case_id,))],
            "reports": [dict(r) for r in conn.execute("SELECT * FROM reports WHERE case_id=? ORDER BY country,id", (case_id,))],
            "reviews": [dict(r) for r in conn.execute("SELECT * FROM medical_reviews WHERE case_id=? ORDER BY id", (case_id,))],
            "audit": audit_rows,
            "timeline": self._timeline(conn, case_id, source_ids, audit_rows, privileged),
        }
        return result

    def _timeline(self, conn: sqlite3.Connection, target_id: int, source_ids: list[int],
                  audit_rows: list[dict[str, Any]], include_audit: bool) -> list[dict[str, Any]]:
        """Build the merged handling timeline. Every moved entry carries its origin case."""
        events: list[dict[str, Any]] = []
        origin_map = {row["id"]: row["case_no"] for row in conn.execute(
            "SELECT id,case_no FROM cases WHERE id IN (%s)" % ",".join("?" for _ in source_ids), source_ids
        )}
        target_no = origin_map.get(target_id)

        def origin_of(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
            origin_id = row["origin_case_id"] if "origin_case_id" in row.keys() else None
            origin_no = row["origin_case_no"] if "origin_case_no" in row.keys() else None
            if origin_id is None:
                origin_id = target_id
            if not origin_no:
                origin_no = origin_map.get(origin_id) or target_no
            return {"origin_case_id": origin_id, "origin_case_no": origin_no,
                    "moved": origin_id != target_id}

        for r in conn.execute(
            f"SELECT * FROM intakes WHERE case_id=? ORDER BY received_at,id", (target_id,)
        ):
            events.append({"type": "intake", "at": r["received_at"], "source": r["source"],
                           "dedupe_key": r["dedupe_key"], "created_by": r["created_by"], **origin_of(r)})
        for r in conn.execute("SELECT * FROM followups WHERE case_id=? ORDER BY received_at,id", (target_id,)):
            events.append({"type": "followup", "at": r["received_at"], "revision": r["revision"],
                           "content": r["content"], "source": r["source"], "created_by": r["created_by"],
                           **origin_of(r)})
        for r in conn.execute("SELECT * FROM medical_reviews WHERE case_id=? ORDER BY created_at,id", (target_id,)):
            events.append({"type": "medical_review", "at": r["created_at"], "case_revision": r["case_revision"],
                           "serious": bool(r["serious"]), "fatal": bool(r["fatal"]),
                           "causality": r["causality"], "rationale": r["rationale"], "reviewer": r["reviewer"],
                           **origin_of(r)})
        for r in conn.execute("SELECT * FROM reports WHERE case_id=? ORDER BY due_at,id", (target_id,)):
            base = origin_of(r)
            events.append({"type": "report_due", "at": r["due_at"], "country": r["country"],
                           "report_id": r["id"], **base})
            if r["status"] == "submitted" and r["submitted_at"]:
                events.append({"type": "report_submitted", "at": r["submitted_at"], "country": r["country"],
                               "report_id": r["id"], "late": bool(r["late"]),
                               "submitted_by": r["submitted_by"], **base})

        if include_audit:
            for row in audit_rows:
                origin_id = row["case_id"]
                events.append({
                    "type": "audit", "at": row["created_at"], "action": row["action"],
                    "actor": row["actor"], "role": row["role"],
                    "detail": json.loads(row["detail_json"]),
                    "origin_case_id": origin_id,
                    "origin_case_no": origin_map.get(origin_id) or target_no,
                    "moved": origin_id != target_id,
                })

        events.sort(key=lambda e: (e["at"] or "", e["type"]))
        return events

    def list_cases(self, role: str, region: str, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        sql = "SELECT * FROM cases WHERE status!='merged'"
        args: list[Any] = []
        if role not in {"medical_reviewer", "global_admin"}:
            sql += " AND region=?"
            args.append(region)
        if query.get("status"):
            sql += " AND status=?"
            args.append(query["status"][0])
        sql += " ORDER BY received_at DESC,id DESC"
        return [dict(r) for r in self.repo.conn.execute(sql, args)]

    def add_followup(self, case_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        content = str(body.get("content", "")).strip()
        source = str(body.get("source", "")).strip()
        if not content or not source:
            raise ApiError(400, "missing_fields", "content 和 source 必填")
        expected = body.get("expected_revision")
        if not isinstance(expected, int):
            raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.tx() as conn:
            case = self._case(conn, case_id)
            if not self.can_access(case, role, region) or role in {"medical_reviewer"}:
                raise ApiError(403, "followup_forbidden", "当前角色不能提交随访")
            if case["status"] == "merged":
                raise ApiError(409, "case_merged", "已合并案例不能再更新")
            if case["revision"] != expected:
                raise ApiError(409, "revision_conflict", "案例已被其他人员更新，请重新读取")
            revision = case["revision"] + 1
            received = parse_time(body.get("received_at"), utcnow())
            due = report_deadline(received, bool(case["serious"]), bool(case["fatal"]))
            conn.execute(
                "INSERT INTO followups(case_id,origin_case_id,origin_case_no,content,source,received_at,revision,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (case_id, case_id, case["case_no"], content, source, iso(received), revision, actor, iso()),
            )
            conn.execute(
                "UPDATE cases SET revision=?,received_at=?,report_due_at=?,updated_at=? WHERE id=?",
                (revision, iso(received), iso(due), iso(), case_id),
            )
            Repository.audit(conn, case_id, actor, role, "followup_added", {"revision": revision, "source": source})
            return {"case": dict(self._case(conn, case_id)), "revision": revision}

    def medical_review(self, case_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "medical_reviewer":
            raise ApiError(403, "medical_reviewer_required", "只有医学审核员可以裁定严重性")
        expected = body.get("expected_revision")
        if not isinstance(expected, int):
            raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        serious = body.get("serious")
        fatal = body.get("fatal")
        causality = str(body.get("causality", "")).strip()
        rationale = str(body.get("rationale", "")).strip()
        if not isinstance(serious, bool) or not isinstance(fatal, bool) or not causality or not rationale:
            raise ApiError(400, "invalid_review", "serious/fatal 必须是布尔值，causality 和 rationale 必填")
        if fatal and not serious:
            raise ApiError(400, "invalid_severity", "死亡案例必须标记为严重")
        received = parse_time(body.get("received_at"))
        due = report_deadline(received, serious, fatal)
        with self.repo.tx() as conn:
            case = self._case(conn, case_id)
            if case["status"] == "merged":
                raise ApiError(409, "case_merged", "已合并案例不能审核")
            if case["revision"] != expected:
                raise ApiError(409, "revision_conflict", "案例版本已变化")
            revision = expected + 1
            conn.execute(
                """UPDATE cases SET serious=?,fatal=?,causality=?,report_due_at=?,revision=?,updated_at=? WHERE id=?""",
                (int(serious), int(fatal), causality, iso(due), revision, iso(), case_id),
            )
            conn.execute(
                """INSERT INTO medical_reviews(case_id,origin_case_id,origin_case_no,case_revision,serious,fatal,causality,rationale,reviewer,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (case_id, case_id, case["case_no"], expected, int(serious), int(fatal), causality, rationale, actor, iso()),
            )
            Repository.audit(conn, case_id, actor, role, "medical_reviewed", {"from_revision": expected, "serious": serious, "fatal": fatal, "causality": causality})
            return {"case": dict(self._case(conn, case_id)), "reviewed_revision": expected}

    def create_report(self, case_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "report_forbidden", "只有区域负责人或全局管理员可以生成报告")
        country = str(body.get("country", "")).strip().upper()
        if not country:
            raise ApiError(400, "country_required", "country 必填")
        with self.repo.tx() as conn:
            case = self._case(conn, case_id)
            if not self.can_access(case, role, region):
                raise ApiError(403, "region_forbidden", "不能为本区域之外案例生成报告")
            if case["status"] == "merged":
                raise ApiError(409, "case_merged", "已合并案例不能再生成报告")
            due = report_deadline(parse_time(case["received_at"]), bool(case["serious"]), bool(case["fatal"]))
            try:
                cur = conn.execute(
                    "INSERT INTO reports(case_id,origin_case_id,origin_case_no,country,due_at,status) VALUES(?,?,?,?,?,?)",
                    (case_id, case_id, case["case_no"], country, iso(due), "pending"),
                )
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "report_exists", "该国家报告已经存在") from exc
            Repository.audit(conn, case_id, actor, role, "report_created", {"report_id": cur.lastrowid, "country": country})
            return dict(conn.execute("SELECT * FROM reports WHERE id=?", (cur.lastrowid,)).fetchone())

    def submit_report(self, report_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "submit_forbidden", "当前角色不能提交监管报告")
        with self.repo.tx() as conn:
            row = conn.execute("SELECT r.*,c.region,c.status AS case_status FROM reports r JOIN cases c ON c.id=r.case_id WHERE r.id=?", (report_id,)).fetchone()
            if not row:
                raise ApiError(404, "report_not_found", "报告不存在")
            if not self.can_access(dict(row), role, region):
                raise ApiError(403, "region_forbidden", "无权提交其他区域报告")
            if row["case_status"] == "merged":
                raise ApiError(409, "case_merged", "已合并案例不能再提交报告")
            if row["status"] == "submitted":
                return {"report": dict(row), "idempotent": True}
            now = parse_time(body.get("submitted_at"), utcnow())
            late = int(now > parse_time(row["due_at"]))
            conn.execute("UPDATE reports SET status='submitted',submitted_at=?,submitted_by=?,late=? WHERE id=?", (iso(now), actor, late, report_id))
            Repository.audit(conn, row["case_id"], actor, role, "report_submitted", {"report_id": report_id, "country": row["country"], "late": bool(late)})
            return {"report": dict(conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()), "idempotent": False}

    def merge_cases(self, source_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "global_admin":
            raise ApiError(403, "merge_forbidden", "只有全局管理员可以合并案例")
        target_id = body.get("target_case_id")
        if not isinstance(target_id, int) or source_id == target_id:
            raise ApiError(400, "invalid_target", "target_case_id 必须指向不同案例")
        with self.repo.tx() as conn:
            source = self._case(conn, source_id)
            target = self._case(conn, target_id)
            if source["status"] == "merged":
                target_case = self._merge_root(conn, source)
                return {"case": dict(target_case), "source_case": dict(source), "idempotent": True,
                        "moved": {"followups": 0, "medical_reviews": 0, "reports": 0, "discarded_reports": []}}
            if target["status"] == "merged" or source["product"].casefold() != target["product"].casefold():
                raise ApiError(409, "merge_conflict", "目标案例不可用，或产品与来源案例不一致")
            now = iso()

            # 1. 接入记录直接随案例迁移；随访和医学审核整体迁移，逐条标注原案例编号。
            conn.execute("UPDATE intakes SET case_id=? WHERE case_id=?", (target_id, source_id))
            followup_count = conn.execute(
                "UPDATE followups SET case_id=? WHERE case_id=?", (target_id, source_id)
            ).rowcount
            review_count = conn.execute(
                "UPDATE medical_reviews SET case_id=? WHERE case_id=?", (target_id, source_id)
            ).rowcount

            # 2. 同国家报告：一份已提交、一份待提交时保留已提交的；两份都待提交时保留截止更早的。
            target_reports = conn.execute("SELECT * FROM reports WHERE case_id=?", (target_id,)).fetchall()
            source_reports = conn.execute("SELECT * FROM reports WHERE case_id=?", (source_id,)).fetchall()
            target_by_country = {r["country"]: r for r in target_reports}
            moved_reports = 0
            discarded: list[dict[str, Any]] = []
            for src in source_reports:
                keep = src
                drop = None
                tgt = target_by_country.get(src["country"])
                if tgt is not None:
                    src_submitted = src["status"] == "submitted"
                    tgt_submitted = tgt["status"] == "submitted"
                    if src_submitted and not tgt_submitted:
                        keep, drop = src, tgt
                    elif tgt_submitted and not src_submitted:
                        keep, drop = tgt, src
                    elif src_submitted and tgt_submitted:
                        # 两份都已提交：保留较早提交的一份，另一份仅作冗余丢弃。
                        keep, drop = (src, tgt) if src["submitted_at"] <= tgt["submitted_at"] else (tgt, src)
                    else:
                        # 两份都待提交（含逾期）：保留截止更早的一份。
                        keep, drop = (src, tgt) if src["due_at"] <= tgt["due_at"] else (tgt, src)
                if drop is not None:
                    discarded.append({"report_id": drop["id"], "country": drop["country"],
                                      "status": drop["status"], "due_at": drop["due_at"],
                                      "reason": "submitted_kept" if keep["status"] == "submitted" else "earlier_due_kept"})
                    conn.execute("DELETE FROM reports WHERE id=?", (drop["id"],))
                if keep is src:
                    conn.execute("UPDATE reports SET case_id=? WHERE id=?", (target_id, src["id"]))
                    target_by_country[src["country"]] = src
                    moved_reports += 1

            # 3. 原案例退出列表（status=merged），此后不能再更新。
            conn.execute(
                "UPDATE cases SET status='merged',merged_into=?,merged_at=?,revision=revision+1,updated_at=? WHERE id=?",
                (target_id, now, now, source_id),
            )
            conn.execute("UPDATE cases SET revision=revision+1,updated_at=? WHERE id=?", (now, target_id))
            Repository.audit(conn, target_id, actor, role, "case_merged_in",
                             {"source_case_id": source_id, "source_case_no": source["case_no"],
                              "moved_followups": followup_count, "moved_medical_reviews": review_count,
                              "moved_reports": moved_reports, "discarded_reports": discarded})
            Repository.audit(conn, source_id, actor, role, "case_merged_into",
                             {"target_case_id": target_id, "target_case_no": target["case_no"]})
            return {"case": dict(self._case(conn, target_id)), "source_case": dict(self._case(conn, source_id)),
                    "idempotent": False,
                    "moved": {"followups": followup_count, "medical_reviews": review_count,
                              "reports": moved_reports, "discarded_reports": discarded}}

    def overdue(self, role: str, region: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reports WHERE status!='submitted' AND due_at < ?"
        args: list[Any] = [iso()]
        if role not in {"medical_reviewer", "global_admin"}:
            sql += " AND case_id IN (SELECT id FROM cases WHERE region=?)"
            args.append(region)
        return [dict(r) for r in self.repo.conn.execute(sql, args)]

    def escalate_overdue(self, actor: str, role: str, region: str) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "escalation_forbidden", "当前角色不能执行逾期升级")
        rows = self.overdue(role, region)
        with self.repo.tx() as conn:
            for row in rows:
                conn.execute("UPDATE reports SET status='overdue' WHERE id=? AND status='pending'", (row["id"],))
                Repository.audit(conn, row["case_id"], actor, role, "report_overdue_escalated", {"report_id": row["id"], "country": row["country"]})
        return {"escalated": len(rows)}

    def state(self, role: str, region: str) -> dict[str, Any]:
        cases = self.list_cases(role, region, {})
        return {"cases": cases, "overdue": self.overdue(role, region), "server_time": iso()}


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: PharmacovigilanceService
    web_root: Path

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            result = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(result, dict):
            raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
        return result

    def _dispatch_get(self, path: str, query: dict[str, list[str]]) -> Any:
        if path == "/health":
            return 200, {"status": "ok", "service": "pharmacovigilance"}
        actor, role, region = self.service.identity(self.headers)
        if path == "/api/state":
            return 200, self.service.state(role, region)
        if path == "/api/cases":
            return 200, {"cases": self.service.list_cases(role, region, query)}
        if path == "/api/overdue":
            return 200, {"reports": self.service.overdue(role, region)}
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[:2] == ["api", "cases"] and parts[2].isdigit():
            return 200, self.service.get_case(int(parts[2]), role, region)
        raise ApiError(404, "not_found", "接口不存在")

    def _dispatch_post(self, path: str, body: dict[str, Any]) -> Any:
        actor, role, region = self.service.identity(self.headers)
        if path == "/api/cases":
            return 201, self.service.create_case(actor, role, region, body)
        if path == "/api/escalate-overdue":
            return 200, self.service.escalate_overdue(actor, role, region)
        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[2].isdigit():
            case_id, action = int(parts[2]), parts[3]
            if action == "followups":
                return 201, self.service.add_followup(case_id, actor, role, region, body)
            if action == "medical-review":
                return 200, self.service.medical_review(case_id, actor, role, body)
            if action == "reports":
                return 201, self.service.create_report(case_id, actor, role, region, body)
            if action == "merge":
                return 200, self.service.merge_cases(case_id, actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "reports"] and parts[2].isdigit() and parts[3] == "submit":
            return 200, self.service.submit_report(int(parts[2]), actor, role, region, body)
        raise ApiError(404, "not_found", "接口不存在")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                page = (self.web_root / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if method == "GET":
                status, payload = self._dispatch_get(parsed.path, parse_qs(parsed.query))
            else:
                status, payload = self._dispatch_post(parsed.path, self._body())
            json_response(self, status, payload)
        except ApiError as exc:
            json_response(self, exc.status, {"error": exc.code, "message": exc.message})
        except Exception as exc:
            print(f"unhandled error: {exc!r}")
            json_response(self, 500, {"error": "internal_error", "message": str(exc)})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = PharmacovigilanceService(db_path)
    web_root = Path(__file__).resolve().parent / "static"
    handler = type("PharmacovigilanceHandler", (Handler,), {"service": service, "web_root": web_root})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--db", default=os.environ.get("PV_DB", "pharmacovigilance.db"))
    args = parser.parse_args()
    server = create_server(args.db, args.host, args.port)
    print(f"pharmacovigilance listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
