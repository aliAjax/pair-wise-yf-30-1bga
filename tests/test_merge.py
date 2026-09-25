import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, utcnow


def case_body(dedupe, product="DrugA", received=None, region="CN", serious=False, fatal=False):
    return {
        "patient_ref": "P-1", "region": region, "product": product, "event_term": "肝损伤",
        "source": "email", "dedupe_key": dedupe,
        "received_at": iso(received or utcnow()), "serious": serious, "fatal": fatal,
    }


class CaseMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.svc = PharmacovigilanceService(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def make_case(self, dedupe, **kwargs):
        return self.svc.create_case("reporter-a", "reporter", kwargs.pop("region", "CN"),
                                    case_body(dedupe, **kwargs))["case"]

    def test_merge_moves_followups_reviews_reports_with_origin_labels(self):
        source = self.make_case("src-1")
        target = self.make_case("tgt-1")
        self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                              {"content": "来源案例随访", "source": "phone", "expected_revision": 1})
        self.svc.medical_review(source["id"], "rev-1", "medical_reviewer",
                                {"expected_revision": 2, "serious": True, "fatal": False,
                                 "causality": "related", "rationale": "来源裁定",
                                 "received_at": iso(utcnow())})
        src_report = self.svc.create_report(source["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.svc.create_report(target["id"], "lead-cn", "regional_lead", "CN", {"country": "US"})
        # 目标案例也有一条 revision 2 的随访，验证不再因 (case_id, revision) 冲突而失败。
        self.svc.add_followup(target["id"], "reporter-b", "reporter", "CN",
                              {"content": "目标案例随访", "source": "email", "expected_revision": 1})

        result = self.svc.merge_cases(source["id"], "admin", "global_admin", {"target_case_id": target["id"]})
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["case"]["id"], target["id"])
        self.assertEqual(result["moved"]["followups"], 1)
        self.assertEqual(result["moved"]["medical_reviews"], 1)

        detail = self.svc.get_case(target["id"], "global_admin", "")
        # 每条迁移记录都标注原案例编号。
        moved_followups = [f for f in detail["followups"] if f["origin_case_id"] == source["id"]]
        self.assertEqual(len(moved_followups), 1)
        self.assertEqual(moved_followups[0]["origin_case_no"], source["case_no"])
        moved_reviews = [r for r in detail["reviews"] if r["origin_case_id"] == source["id"]]
        self.assertEqual(len(moved_reviews), 1)
        self.assertEqual(moved_reviews[0]["origin_case_no"], source["case_no"])
        report_countries = {r["country"] for r in detail["reports"]}
        self.assertEqual(report_countries, {"CN", "US"})
        self.assertEqual(src_report["id"], next(r["id"] for r in detail["reports"] if r["country"] == "CN"))
        # 接入记录同样带原案例标记。
        self.assertTrue(any(i["origin_case_id"] == source["id"] for i in detail["intakes"]))

        # 合并来源列表与统一时间线同时返回。
        self.assertEqual([s["case_no"] for s in detail["merge_sources"]], [source["case_no"]])
        moved_events = [e for e in detail["timeline"] if e["moved"]]
        self.assertTrue(moved_events)
        self.assertTrue(all(e["origin_case_no"] == source["case_no"] for e in moved_events))
        types = {e["type"] for e in detail["timeline"]}
        self.assertIn("followup", types)
        self.assertIn("medical_review", types)
        self.assertIn("intake", types)
        own = [e for e in detail["timeline"] if not e["moved"]]
        self.assertTrue(all(e["origin_case_id"] == target["id"] for e in own))
        # 审计中包含来源与目标两侧的合并记录。
        actions = {a["action"] for a in detail["audit"]}
        self.assertIn("case_merged_in", actions)
        self.assertIn("case_merged_into", actions)

    def test_same_country_submitted_report_wins_over_pending(self):
        for source_submitted in (True, False):
            tmp = tempfile.TemporaryDirectory()
            svc = PharmacovigilanceService(Path(tmp.name) / "t.db")
            source = svc.create_case("ra", "reporter", "CN", case_body(f"s-{source_submitted}"))["case"]
            target = svc.create_case("rb", "reporter", "CN", case_body(f"t-{source_submitted}"))["case"]
            r_src = svc.create_report(source["id"], "lead", "regional_lead", "CN", {"country": "JP"})
            r_tgt = svc.create_report(target["id"], "lead", "regional_lead", "CN", {"country": "JP"})
            if source_submitted:
                svc.submit_report(r_src["id"], "lead", "regional_lead", "CN", {})
            else:
                svc.submit_report(r_tgt["id"], "lead", "regional_lead", "CN", {})
            svc.merge_cases(source["id"], "admin", "global_admin", {"target_case_id": target["id"]})
            reports = svc.get_case(target["id"], "global_admin", "")["reports"]
            jp = [r for r in reports if r["country"] == "JP"]
            self.assertEqual(len(jp), 1)
            self.assertEqual(jp[0]["status"], "submitted")
            expected_id = r_src["id"] if source_submitted else r_tgt["id"]
            self.assertEqual(jp[0]["id"], expected_id)
            tmp.cleanup()

    def test_same_country_both_pending_keeps_earlier_due(self):
        # 用不同的接收时间制造截止先后。
        earlier = self.svc.create_case(
            "reporter-a", "reporter", "CN",
            case_body("earlier", received=utcnow().replace(microsecond=0)),
        )["case"]
        later = self.svc.create_case(
            "reporter-a", "reporter", "CN",
            case_body("later", received=utcnow().replace(microsecond=0)),
        )["case"]
        # 直接改库制造明确的截止先后，避免同秒创建导致并列。
        with self.svc.repo.tx() as conn:
            conn.execute("UPDATE cases SET received_at=? WHERE id=?", ("2026-01-01T00:00:00Z", earlier["id"]))
            conn.execute("UPDATE cases SET received_at=? WHERE id=?", ("2026-02-01T00:00:00Z", later["id"]))
        r_early = self.svc.create_report(earlier["id"], "lead", "regional_lead", "CN", {"country": "DE"})
        r_late = self.svc.create_report(later["id"], "lead", "regional_lead", "CN", {"country": "DE"})
        conn = self.svc.repo.conn
        conn.execute("UPDATE reports SET due_at='2026-04-01T00:00:00Z' WHERE id=?", (r_early["id"],))
        conn.execute("UPDATE reports SET due_at='2026-05-01T00:00:00Z' WHERE id=?", (r_late["id"],))

        # 来源是截止更晚的一份：应被丢弃，目标保留更早的。
        result = self.svc.merge_cases(later["id"], "admin", "global_admin", {"target_case_id": earlier["id"]})
        discarded = result["moved"]["discarded_reports"]
        self.assertEqual(len(discarded), 1)
        self.assertEqual(discarded[0]["country"], "DE")
        self.assertEqual(discarded[0]["reason"], "earlier_due_kept")
        reports = self.svc.get_case(earlier["id"], "global_admin", "")["reports"]
        de = [r for r in reports if r["country"] == "DE"]
        self.assertEqual(len(de), 1)
        self.assertEqual(de[0]["id"], r_early["id"])
        self.assertEqual(de[0]["status"], "pending")

    def test_source_leaves_list_and_is_locked(self):
        source = self.make_case("lock-s")
        target = self.make_case("lock-t")
        self.svc.create_report(source["id"], "lead", "regional_lead", "CN", {"country": "FR"})
        self.svc.merge_cases(source["id"], "admin", "global_admin", {"target_case_id": target["id"]})

        ids = {c["id"] for c in self.svc.list_cases("global_admin", "", {})}
        self.assertNotIn(source["id"], ids)
        self.assertIn(target["id"], ids)

        # 原案例只读：随访、审核、生成报告都被拒绝。
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                                  {"content": "x", "source": "email", "expected_revision": 1})
        self.assertEqual(ctx.exception.code, "case_merged")
        with self.assertRaises(ApiError) as ctx:
            self.svc.medical_review(source["id"], "rev-1", "medical_reviewer",
                                    {"expected_revision": 1, "serious": False, "fatal": False,
                                     "causality": "unrelated", "rationale": "x",
                                     "received_at": iso(utcnow())})
        self.assertEqual(ctx.exception.code, "case_merged")
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_report(source["id"], "lead", "regional_lead", "CN", {"country": "IT"})
        self.assertEqual(ctx.exception.code, "case_merged")

        # 详情仍可读，且指向合并目标。
        detail = self.svc.get_case(source["id"], "global_admin", "")
        self.assertEqual(detail["case"]["status"], "merged")
        self.assertEqual(detail["merged_target"]["id"], target["id"])

        # 重复合并是幂等的。
        again = self.svc.merge_cases(source["id"], "admin", "global_admin", {"target_case_id": target["id"]})
        self.assertTrue(again["idempotent"])

    def test_merge_permissions_and_product_check(self):
        source = self.make_case("perm-s")
        target = self.make_case("perm-t")
        other = self.make_case("perm-o", product="DrugB")
        with self.assertRaises(ApiError) as ctx:
            self.svc.merge_cases(source["id"], "lead", "regional_lead", {"target_case_id": target["id"]})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.merge_cases(source["id"], "admin", "global_admin", {"target_case_id": other["id"]})
        self.assertEqual(ctx.exception.code, "merge_conflict")

    def test_chained_merge_keeps_full_lineage(self):
        a = self.make_case("chain-a")
        b = self.make_case("chain-b")
        c = self.make_case("chain-c")
        self.svc.add_followup(a["id"], "reporter-a", "reporter", "CN",
                              {"content": "A 的随访", "source": "phone", "expected_revision": 1})
        self.svc.merge_cases(a["id"], "admin", "global_admin", {"target_case_id": b["id"]})
        self.svc.merge_cases(b["id"], "admin", "global_admin", {"target_case_id": c["id"]})

        detail = self.svc.get_case(c["id"], "global_admin", "")
        source_nos = {s["case_no"] for s in detail["merge_sources"]}
        self.assertEqual(source_nos, {a["case_no"], b["case_no"]})
        indirect = {s["case_no"]: s for s in detail["merge_sources"]}[a["case_no"]]
        self.assertTrue(indirect["indirect"])
        self.assertEqual(indirect["via_case_no"], b["case_no"])
        # A 的随访、A/B 两侧的审计都出现在最终时间线。
        followups = {e["origin_case_no"] for e in detail["timeline"] if e["type"] == "followup"}
        self.assertEqual(followups, {a["case_no"]})
        actions = {(e["origin_case_no"], e["action"]) for e in detail["timeline"] if e["type"] == "audit"}
        self.assertIn((a["case_no"], "followup_added"), actions)
        self.assertIn((a["case_no"], "case_merged_into"), actions)
        self.assertIn((b["case_no"], "case_merged_into"), actions)

        # 读取 A 也能沿合并链找到最终目标。
        a_detail = self.svc.get_case(a["id"], "global_admin", "")
        self.assertEqual(a_detail["merged_target"]["id"], c["id"])

    def test_legacy_database_migrates_and_merges(self):
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "legacy.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_no TEXT NOT NULL UNIQUE, patient_ref TEXT NOT NULL,
                region TEXT NOT NULL, product TEXT NOT NULL, event_term TEXT NOT NULL, onset_at TEXT,
                received_at TEXT NOT NULL, serious INTEGER NOT NULL DEFAULT 0, fatal INTEGER NOT NULL DEFAULT 0,
                causality TEXT, report_due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                revision INTEGER NOT NULL DEFAULT 1, merged_into INTEGER, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE intakes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER REFERENCES cases(id), source TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, received_at TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE followups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
                content TEXT NOT NULL, source TEXT NOT NULL, received_at TEXT NOT NULL, revision INTEGER NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(case_id, revision));
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
                country TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                submitted_at TEXT, submitted_by TEXT, late INTEGER NOT NULL DEFAULT 0, UNIQUE(case_id, country));
            CREATE TABLE medical_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
                case_revision INTEGER NOT NULL, serious INTEGER NOT NULL, fatal INTEGER NOT NULL,
                causality TEXT NOT NULL, rationale TEXT NOT NULL, reviewer TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(case_id, case_revision));
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL,
                action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO cases VALUES(1,'PV-OLD-1','P','CN','DrugA','肝损',NULL,'2026-01-01T00:00:00Z',0,0,
                NULL,'2026-04-01T00:00:00Z','open',1,NULL,'r','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
            INSERT INTO followups(case_id,content,source,received_at,revision,created_by,created_at)
                VALUES(1,'旧随访','email','2026-01-02T00:00:00Z',2,'r','2026-01-02T00:00:00Z');
            """
        )
        conn.commit()
        conn.close()

        svc = PharmacovigilanceService(db)
        detail = svc.get_case(1, "global_admin", "")
        f = detail["followups"][0]
        self.assertEqual(f["origin_case_id"], 1)
        self.assertEqual(f["origin_case_no"], "PV-OLD-1")

        target = svc.create_case("r2", "reporter", "CN", case_body("legacy-new"))["case"]
        result = svc.merge_cases(1, "admin", "global_admin", {"target_case_id": target["id"]})
        self.assertEqual(result["moved"]["followups"], 1)
        merged_detail = svc.get_case(target["id"], "global_admin", "")
        self.assertEqual(merged_detail["followups"][0]["origin_case_no"], "PV-OLD-1")
        self.assertTrue(merged_detail["followups"][0]["id"] == f["id"])
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
