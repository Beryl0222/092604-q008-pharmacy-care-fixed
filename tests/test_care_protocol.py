import tempfile
import unittest

from pharmacy_care.clock import Clock
from pharmacy_care.domain import ConflictError, DomainError
from pharmacy_care.service import Service
from pharmacy_care.store import Store

NOW = "2026-01-10T09:00:00+00:00"
PAST = "2026-01-05T09:00:00+00:00"
FUTURE = "2026-02-01T09:00:00+00:00"
VALID_FROM = "2026-01-01T00:00:00+00:00"
VALID_UNTIL = "2027-01-01T00:00:00+00:00"


class FixedClock(Clock):
    def __init__(self, now=NOW):
        self._now = now

    def now(self):
        return self._now


def make_service(store=None, quota_total=5):
    service = Service(store or Store(), FixedClock())
    service.create_protocol("p1", "patient-1", "store-a", quota_total=quota_total)
    service.grant_consent("c1", "p1", ["reminder", "reconciliation", "lifestyle"])
    service.register_practitioner(
        "ph1", "王药师", "pharmacist", "licensed", "store-a", VALID_FROM, VALID_UNTIL
    )
    service.register_practitioner(
        "ph2", "李药师", "pharmacist", "licensed", "store-b", VALID_FROM, VALID_UNTIL
    )
    service.register_practitioner(
        "sales1", "赵销售", "sales", "none", "store-a", VALID_FROM, VALID_UNTIL
    )
    service.add_medication_list("p1", [{"drug": "metformin", "dose": "0.5g"}])
    return service


class 回访签署与资格测试(unittest.TestCase):
    def test_合格药师签署回访并结算额度(self):
        service = make_service()
        service.schedule_followup("f1", "p1", FUTURE, "bk-f1")
        done = service.complete_followup("f1", "ph1", note="核对药物清单")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["signed_by"], "ph1")
        self.assertEqual(service.pharmacist_view("p1")["quota_available"], 4)

    def test_资格过期药师不能签署(self):
        service = make_service()
        service.register_practitioner(
            "ph9", "过期药师", "pharmacist", "licensed", "store-a",
            VALID_FROM, "2026-01-02T00:00:00+00:00",
        )
        service.schedule_followup("f1", "p1", FUTURE, "bk-f1")
        with self.assertRaises(DomainError):
            service.complete_followup("f1", "ph9")
        self.assertEqual(service.pharmacist_view("p1")["quota_available"], 5)

    def test_销售不能签署回访(self):
        service = make_service()
        service.schedule_followup("f1", "p1", FUTURE, "bk-f1")
        with self.assertRaises(DomainError):
            service.complete_followup("f1", "sales1")


class 冻结规则测试(unittest.TestCase):
    def test_新处方冻结依赖旧清单的未来回访且保留历史(self):
        service = make_service()
        service.schedule_followup("f1", "p1", FUTURE, "bk-f1")
        service.add_medication_list("p1", [{"drug": "metformin", "dose": "1.0g"}])
        followup = service.pharmacist_view("p1")["followups"][0]
        self.assertEqual(followup["status"], "frozen")
        with self.assertRaises(DomainError):
            service.complete_followup("f1", "ph1")
        versions = service.compliance_view("p1")["medication_lists"]
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(versions[0]["status"], "superseded")

    def test_撤回同意冻结协议与未来动作且保留历史(self):
        service = make_service()
        service.schedule_followup("f1", "p1", FUTURE, "bk-f1")
        service.withdraw_consent("p1")
        self.assertEqual(service.get_protocol("p1")["state"], "frozen")
        self.assertEqual(
            service.pharmacist_view("p1")["followups"][0]["status"], "frozen"
        )
        with self.assertRaises(DomainError):
            service.schedule_followup("f2", "p1", FUTURE, "bk-f2")
        history = service.compliance_view("p1")["consent_history"]
        self.assertEqual(history[0]["status"], "withdrawn")
        self.assertIsNotNone(history[0]["withdrawn_at"])


class 异常升级测试(unittest.TestCase):
    def test_销售不能关闭安全异常_药师可以(self):
        service = make_service()
        service.open_escalation("e1", "p1", "safety", "疑似不良反应")
        with self.assertRaises(DomainError):
            service.close_escalation("e1", "sales1")
        closed = service.close_escalation("e1", "ph1")
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["closed_by"], "ph1")


class 额度事务测试(unittest.TestCase):
    def test_预占与退回(self):
        service = make_service()
        hold = service.hold_quota("p1", 2, note="预占两次随访")
        self.assertEqual(service.pharmacist_view("p1")["quota_available"], 3)
        service.release_quota(hold["entry_id"])
        self.assertEqual(service.pharmacist_view("p1")["quota_available"], 5)

    def test_额度不足时签署整体回滚(self):
        service = make_service(quota_total=1)
        service.schedule_followup("f1", "p1", FUTURE, "bk-f1")
        service.schedule_followup("f2", "p1", FUTURE, "bk-f2")
        service.complete_followup("f1", "ph1")
        with self.assertRaises(DomainError):
            service.complete_followup("f2", "ph1")
        followups = {f["followup_id"]: f for f in service.pharmacist_view("p1")["followups"]}
        self.assertEqual(followups["f2"]["status"], "scheduled")
        self.assertIsNone(followups["f2"]["signed_by"])

    def test_完成优先冲抵对应预占(self):
        service = make_service()
        service.schedule_followup("f1", "p1", FUTURE, "bk-f1")
        service.hold_quota("p1", 1, ref_id="f1", note="为 f1 预占")
        service.complete_followup("f1", "ph1")
        ledger = service.compliance_view("p1")["quota_ledger"]
        consume = [e for e in ledger if e["kind"] == "consume"][0]
        hold = [e for e in ledger if e["kind"] == "hold"][0]
        self.assertEqual(consume["ref_id"], hold["entry_id"])


class 跨店交接测试(unittest.TestCase):
    def test_转店后原门店不能继续消费额度(self):
        service = make_service()
        service.schedule_followup("f1", "p1", FUTURE, "bk-f1")
        service.hold_quota("p1", 2, note="原门店预占")
        handoff = service.transfer_store("h1", "p1", "store-b")
        self.assertEqual(handoff["from_store"], "store-a")
        # 原门店预占已在转店事务中释放
        self.assertEqual(service.pharmacist_view("p1")["quota_available"], 5)
        with self.assertRaises(DomainError):
            service.complete_followup("f1", "ph1")
        done = service.complete_followup("f1", "ph2")
        self.assertEqual(done["signed_by"], "ph2")
        self.assertEqual(service.get_protocol("p1")["store_id"], "store-b")


class 幂等与复核测试(unittest.TestCase):
    def test_离线补录按业务键去重_冲突进入复核(self):
        service = make_service()
        created = service.backfill_followup("f1", "bk-off-1", "p1", "ph1", PAST, "门店离线记录")
        replayed = service.backfill_followup("f-x", "bk-off-1", "p1", "ph1", PAST, "门店离线记录")
        self.assertEqual(replayed["followup_id"], created["followup_id"])
        with self.assertRaises(ConflictError):
            service.backfill_followup("f-y", "bk-off-1", "p1", "ph1", PAST, "内容不一致")
        reviews = service.compliance_view("p1")["reviews"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["category"], "backfill")

    def test_支付回调按业务键去重_冲突进入复核(self):
        service = make_service()
        payload = {"order": "o-1", "channel": "wechat"}
        created = service.record_payment("pay1", "p1", "bk-pay-1", 100, payload)
        replayed = service.record_payment("pay2", "p1", "bk-pay-1", 100, payload)
        self.assertEqual(replayed["payment_id"], created["payment_id"])
        with self.assertRaises(ConflictError):
            service.record_payment("pay3", "p1", "bk-pay-1", 200, payload)
        reviews = service.compliance_view("p1")["reviews"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["category"], "payment")

    def test_回访业务键冲突进入复核(self):
        service = make_service()
        service.schedule_followup("f1", "p1", FUTURE, "bk-f1")
        with self.assertRaises(ConflictError):
            service.schedule_followup("f2", "p1", PAST, "bk-f1")


class 分角色视图测试(unittest.TestCase):
    def test_患者_药师_合规看到各自有权看到的内容(self):
        service = make_service()
        service.add_goal("g1", "p1", "三个月内规律用药")
        service.schedule_followup("f1", "p1", FUTURE, "bk-f1")
        service.open_escalation("e1", "p1", "safety", "疑似不良反应")
        service.record_payment("pay1", "p1", "bk-pay-1", 100, {"order": "o-1"})

        patient = service.patient_view("p1")
        self.assertEqual(patient["goals"][0]["description"], "三个月内规律用药")
        self.assertEqual(patient["consent"]["status"], "granted")
        for hidden in ("open_escalations", "payments", "quota_ledger", "reviews"):
            self.assertNotIn(hidden, patient)

        pharmacist = service.pharmacist_view("p1")
        self.assertEqual(len(pharmacist["open_escalations"]), 1)
        self.assertEqual(len(pharmacist["medication_lists"]), 1)
        self.assertNotIn("payments", pharmacist)
        self.assertNotIn("quota_ledger", pharmacist)

        compliance = service.compliance_view("p1")
        self.assertEqual(len(compliance["payments"]), 1)
        self.assertEqual(len(compliance["escalations"]), 1)
        self.assertIn("quota_ledger", compliance)
        self.assertIn("consent_history", compliance)


class 重启恢复测试(unittest.TestCase):
    def test_重启后恢复逾期回访与升级队列(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            service = make_service(store=Store(tmp.name))
            service.schedule_followup("f1", "p1", PAST, "bk-f1")
            service.schedule_followup("f2", "p1", FUTURE, "bk-f2")
            service.open_escalation("e1", "p1", "safety", "疑似不良反应")

            restarted = Service(Store(tmp.name), FixedClock())
            recovered = restarted.recover()
            self.assertEqual(
                [f["followup_id"] for f in recovered["overdue_followups"]], ["f1"]
            )
            self.assertEqual(
                [e["escalation_id"] for e in recovered["open_escalations"]], ["e1"]
            )


if __name__ == "__main__":
    unittest.main()
