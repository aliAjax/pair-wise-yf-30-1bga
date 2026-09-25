import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, utcnow


def at(day, hour=12):
    return iso(datetime(2026, 9, day, hour, tzinfo=timezone.utc))


class PharmacovigilanceFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, dedupe="intake-1"):
        return self.svc.create_case(
            "reporter-a", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "email", "dedupe_key": dedupe, "received_at": iso(utcnow()), "serious": False},
        )["case"]

    def test_full_case_and_deduplication_flow(self):
        case = self.create()
        self.assertEqual(case["revision"], 1)
        followed = self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "住院并出现死亡转归", "source": "phone", "expected_revision": 1,
             "received_at": iso(utcnow())},
        )
        self.assertEqual(followed["revision"], 2)
        reviewed = self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": 2, "serious": True, "fatal": True, "causality": "possibly_related",
             "rationale": "住院记录和死亡证明已核验", "received_at": iso(utcnow())},
        )
        self.assertEqual(reviewed["case"]["revision"], 3)
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        submitted = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(submitted["report"]["status"], "submitted")
        duplicate = self.svc.create_case(
            "reporter-b", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "fax", "dedupe_key": "intake-1", "received_at": iso(utcnow())},
        )
        self.assertTrue(duplicate["deduplicated"])
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(len(detail["intakes"]), 1)
        self.assertGreaterEqual(len(detail["audit"]), 5)
        self.assertEqual(submitted["report"]["late"], 0)

    def test_permissions_and_stale_revision(self):
        case = self.create("intake-2")
        with self.assertRaises(ApiError) as ctx:
            self.svc.get_case(case["id"], "reporter", "US")
        self.assertEqual(ctx.exception.status, 403)
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "第一次更新", "source": "email", "expected_revision": 1})
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                                  {"content": "过期修改", "source": "email", "expected_revision": 1})
        self.assertEqual(ctx.exception.code, "revision_conflict")
        with self.assertRaises(ApiError) as ctx:
            self.svc.medical_review(case["id"], "lead-cn", "regional_lead",
                                    {"expected_revision": 2, "serious": True, "fatal": False,
                                     "causality": "related", "rationale": "x", "received_at": iso(utcnow())})
        self.assertEqual(ctx.exception.status, 403)


class CaseMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "merge.db")

    def tearDown(self):
        self.tmp.cleanup()

    def make_case(self, case_no, dedupe, product="DrugA", region="CN", serious=False, day=1):
        return self.svc.create_case(
            "reporter-a", "reporter", region,
            {"patient_ref": "P-1", "region": region, "product": product, "event_term": "肝损伤",
             "source": "email", "dedupe_key": dedupe, "case_no": case_no,
             "received_at": at(day), "serious": serious},
        )["case"]

    def report(self, case_id, country, role="global_admin", region=""):
        return self.svc.create_report(case_id, "admin", role, region, {"country": country})

    def merge(self, source_id, target_id, role="global_admin"):
        return self.svc.merge_cases(source_id, "admin", role, {"target_case_id": target_id})

    def test_history_moves_to_target_with_origin_case_no(self):
        source = self.make_case("PV-SRC", "d-src", serious=True)
        target = self.make_case("PV-TGT", "d-tgt", day=2)
        self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                              {"content": "来源随访", "source": "phone", "expected_revision": 1, "received_at": at(3)})
        self.svc.medical_review(source["id"], "reviewer-1", "medical_reviewer",
                                {"expected_revision": 2, "serious": True, "fatal": False,
                                 "causality": "related", "rationale": "来源依据", "received_at": at(4)})

        result = self.merge(source["id"], target["id"])
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["moved"], {"followups": 1, "reviews": 1, "reports": 0, "intakes": 1})

        detail = self.svc.get_case(target["id"], "global_admin", "")
        self.assertEqual(len(detail["followups"]), 1)
        self.assertEqual(detail["followups"][0]["origin_case_no"], "PV-SRC")
        self.assertEqual(detail["followups"][0]["revision"], 2)
        self.assertEqual(len(detail["reviews"]), 1)
        self.assertEqual(detail["reviews"][0]["origin_case_no"], "PV-SRC")
        self.assertEqual(len(detail["intakes"]), 2)  # 双方接入记录都汇入目标
        self.assertEqual([s["case_no"] for s in detail["merged_sources"]], ["PV-SRC"])

        source_detail = self.svc.get_case(source["id"], "global_admin", "")
        self.assertEqual(source_detail["followups"], [])
        self.assertEqual(source_detail["reports"], [])
        self.assertEqual(source_detail["reviews"], [])

        # 时间线包含来源录入、迁移来的随访和审核，并标注原案例编号
        origins = {e["type"]: e["origin_case_no"] for e in detail["timeline"]}
        self.assertEqual(origins["followup"], "PV-SRC")
        self.assertEqual(origins["medical_review"], "PV-SRC")
        self.assertEqual(origins["case_merged"], "PV-SRC")
        self.assertIn(("case_created", "PV-SRC"),
                      {(e["type"], e["origin_case_no"]) for e in detail["timeline"]})

    def test_duplicate_country_report_keeps_submitted_side(self):
        source = self.make_case("PV-S2", "d-2", serious=True)
        target = self.make_case("PV-T2", "d-3", day=2, serious=True)
        submitted = self.report(source["id"], "CN")
        self.svc.submit_report(submitted["id"], "lead-cn", "regional_lead", "CN", {"submitted_at": at(3)})
        self.report(target["id"], "CN")  # 目标侧仍待提交

        result = self.merge(source["id"], target["id"])
        detail = self.svc.get_case(target["id"], "global_admin", "")
        cn = [r for r in detail["reports"] if r["country"] == "CN"]
        self.assertEqual(len(cn), 1)
        self.assertEqual(cn[0]["status"], "submitted")
        self.assertEqual(cn[0]["submitted_by"], "lead-cn")
        self.assertEqual(cn[0]["origin_case_no"], "PV-S2")
        self.assertEqual(result["reports_dropped"], 1)

    def test_duplicate_country_report_both_pending_keeps_earlier_due(self):
        # 目标接收更早 -> 目标截止更早 -> 保留目标
        source = self.make_case("PV-S3", "d-4", serious=True, day=10)
        target = self.make_case("PV-T3", "d-5", day=1, serious=True)
        self.report(source["id"], "US")
        self.report(target["id"], "US")

        self.merge(source["id"], target["id"])
        detail = self.svc.get_case(target["id"], "global_admin", "")
        us = [r for r in detail["reports"] if r["country"] == "US"]
        self.assertEqual(len(us), 1)
        self.assertEqual(us[0]["status"], "pending")
        self.assertIsNone(us[0]["origin_case_no"])

        # 反向：来源截止更早时保留来源
        self.tmp2 = tempfile.TemporaryDirectory()
        svc2 = PharmacovigilanceService(Path(self.tmp2.name) / "reverse.db")
        s = svc2.create_case("a", "reporter", "CN", {"patient_ref": "P", "region": "CN", "product": "D",
            "event_term": "x", "source": "e", "dedupe_key": "k1", "case_no": "PV-A",
            "received_at": at(1), "serious": True})["case"]
        tgt = svc2.create_case("a", "reporter", "CN", {"patient_ref": "P", "region": "CN", "product": "D",
            "event_term": "x", "source": "e", "dedupe_key": "k2", "case_no": "PV-B",
            "received_at": at(10), "serious": True})["case"]
        svc2.create_report(s["id"], "admin", "global_admin", "", {"country": "DE"})
        svc2.create_report(tgt["id"], "admin", "global_admin", "", {"country": "DE"})
        svc2.merge_cases(s["id"], "admin", "global_admin", {"target_case_id": tgt["id"]})
        de = [r for r in svc2.get_case(tgt["id"], "global_admin", "")["reports"] if r["country"] == "DE"]
        self.assertEqual(de[0]["origin_case_no"], "PV-A")
        self.tmp2.cleanup()

    def test_merged_case_leaves_list_and_is_locked(self):
        source = self.make_case("PV-S4", "d-6", day=1)
        target = self.make_case("PV-T4", "d-7", day=2)
        self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                              {"content": "x", "source": "email", "expected_revision": 1, "received_at": at(3)})
        self.merge(source["id"], target["id"])

        self.assertNotIn("PV-S4", [c["case_no"] for c in self.svc.list_cases("global_admin", "", {})])
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                                  {"content": "锁后随访", "source": "email",
                                   "expected_revision": 2, "received_at": at(4)})
        self.assertEqual(ctx.exception.code, "case_merged")
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_report(source["id"], "admin", "global_admin", "", {"country": "JP"})
        self.assertEqual(ctx.exception.code, "case_merged")
        with self.assertRaises(ApiError) as ctx:
            self.svc.medical_review(source["id"], "reviewer-1", "medical_reviewer",
                                    {"expected_revision": 2, "serious": False, "fatal": False,
                                     "causality": "x", "rationale": "y", "received_at": at(4)})
        self.assertEqual(ctx.exception.code, "case_merged")
        # 重复合并不再搬移数据
        again = self.merge(source["id"], target["id"])
        self.assertTrue(again["idempotent"])

    def test_merge_requires_same_product_and_global_admin(self):
        source = self.make_case("PV-S5", "d-8", product="DrugA")
        other = self.make_case("PV-O5", "d-9", product="DrugB", day=2)
        with self.assertRaises(ApiError) as ctx:
            self.merge(source["id"], other["id"])
        self.assertEqual(ctx.exception.code, "merge_conflict")
        with self.assertRaises(ApiError) as ctx:
            self.merge(source["id"], other["id"], role="regional_lead")
        self.assertEqual(ctx.exception.status, 403)

    def test_chained_merge_keeps_earliest_origin_tag(self):
        first = self.make_case("PV-C1", "d-10", serious=True, day=1)
        middle = self.make_case("PV-C2", "d-11", serious=True, day=2)
        last = self.make_case("PV-C3", "d-12", serious=True, day=3)
        self.svc.add_followup(first["id"], "reporter-a", "reporter", "CN",
                              {"content": "最早随访", "source": "email", "expected_revision": 1, "received_at": at(4)})
        self.merge(first["id"], middle["id"])
        self.merge(middle["id"], last["id"])

        detail = self.svc.get_case(last["id"], "global_admin", "")
        self.assertEqual({s["case_no"] for s in detail["merged_sources"]}, {"PV-C1", "PV-C2"})
        self.assertEqual(detail["followups"][0]["origin_case_no"], "PV-C1")
        self.assertEqual(len(detail["followups"]), 1)
        # 目标自身后续随访仍可添加（部分唯一索引不限制迁移记录）
        followed = self.svc.add_followup(last["id"], "reporter-a", "reporter", "CN",
                                         {"content": "目标随访", "source": "email",
                                          "expected_revision": detail["case"]["revision"], "received_at": at(5)})
        self.assertEqual(followed["revision"], detail["case"]["revision"] + 1)


class LegacySchemaMigrationTest(unittest.TestCase):
    def test_old_schema_without_origin_columns_is_migrated(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "legacy.db"
        # 按迁移前的旧结构建库并写入一条随访（旧表级 UNIQUE(case_id,revision)）
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE cases (id INTEGER PRIMARY KEY AUTOINCREMENT, case_no TEXT UNIQUE, patient_ref TEXT,
                region TEXT, product TEXT, event_term TEXT, onset_at TEXT, received_at TEXT,
                serious INTEGER DEFAULT 0, fatal INTEGER DEFAULT 0, causality TEXT, report_due_at TEXT,
                status TEXT DEFAULT 'open', revision INTEGER DEFAULT 1, merged_into INTEGER,
                created_by TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE intakes (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, source TEXT,
                dedupe_key TEXT UNIQUE, payload_json TEXT, received_at TEXT, created_by TEXT, created_at TEXT);
            CREATE TABLE followups (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, content TEXT,
                source TEXT, received_at TEXT, revision INTEGER, created_by TEXT, created_at TEXT,
                UNIQUE(case_id, revision));
            CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, country TEXT,
                due_at TEXT, status TEXT DEFAULT 'pending', submitted_at TEXT, submitted_by TEXT,
                late INTEGER DEFAULT 0, UNIQUE(case_id, country));
            CREATE TABLE medical_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER,
                case_revision INTEGER, serious INTEGER, fatal INTEGER, causality TEXT, rationale TEXT,
                reviewer TEXT, created_at TEXT, UNIQUE(case_id, case_revision));
            CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, actor TEXT,
                role TEXT, action TEXT, detail_json TEXT, created_at TEXT);
            INSERT INTO cases(case_no,patient_ref,region,product,event_term,received_at,report_due_at,
                revision,created_by,created_at,updated_at)
                VALUES ('PV-OLD','P','CN','D','x','2026-09-01T00:00:00Z','2026-11-30T00:00:00Z',
                        1,'a','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z');
            INSERT INTO followups(case_id,content,source,received_at,revision,created_by,created_at)
                VALUES (1,'旧随访','email','2026-09-02T00:00:00Z',1,'a','2026-09-02T00:00:00Z');
            """
        )
        conn.commit()
        conn.close()

        svc = PharmacovigilanceService(db_path)  # 触发迁移
        target = svc.create_case("a", "reporter", "CN",
                                 {"patient_ref": "P", "region": "CN", "product": "D", "event_term": "x",
                                  "source": "email", "dedupe_key": "new", "case_no": "PV-NEW",
                                  "received_at": at(3)})["case"]
        result = svc.merge_cases(1, "admin", "global_admin", {"target_case_id": target["id"]})
        self.assertEqual(result["moved"]["followups"], 1)
        detail = svc.get_case(target["id"], "global_admin", "")
        self.assertEqual(detail["followups"][0]["content"], "旧随访")
        self.assertEqual(detail["followups"][0]["origin_case_no"], "PV-OLD")
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
