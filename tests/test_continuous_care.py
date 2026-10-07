"""连续照护协议的业务规则测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from pharmacy_care.api import handle
from pharmacy_care.clock import Clock
from pharmacy_care.service import Service, ServiceError
from pharmacy_care.store import Store

T0 = "2026-10-07T09:00:00+00:00"
T1 = "2026-10-08T09:00:00+00:00"
T2 = "2026-10-09T09:00:00+00:00"


class FixedClock(Clock):
    def __init__(self, moment=T0):
        self.moment = moment

    def now(self):
        return self.moment


def make_service(moment=T0, store=None):
    return Service(store or Store(), FixedClock(moment))


def make_full(moment=T0):
    """建好人员、协议、清单与额度的服务实例。"""
    service = make_service(moment)
    service.register_staff(
        "ph-1", "pharmacist",
        qualifications=["licensed_pharmacist"],
        store_ids=["store-a", "store-b"],
        valid_from="2026-01-01T00:00:00+00:00",
        valid_until="2026-12-31T23:59:59+00:00",
    )
    service.register_staff("sales-1", "sales", store_ids=["store-a"])
    service.register_staff("co-1", "compliance")
    service.open_agreement(
        "ag-1", "pat-1", "store-a", ["reminder", "med_review", "lifestyle"]
    )
    service.record_medication_list("ag-1", [{"name": "阿司匹林", "dose": "100mg"}])
    service.payment_callback("ag-1", 5, request_key="pay-1")
    return service


class 签署资格测试(unittest.TestCase):
    def test_回访只能由当时具备资格且获门店授权的药师签署(self):
        service = make_full()
        service.schedule_followup("ag-1", "f-1", "reminder", T1)

        with self.assertRaises(ServiceError):
            service.sign_followup("f-1", "sales-1")  # 销售不能签署

        service.register_staff(
            "ph-expired", "pharmacist",
            qualifications=["licensed_pharmacist"],
            store_ids=["store-a"],
            valid_from="2020-01-01T00:00:00+00:00",
            valid_until="2020-12-31T23:59:59+00:00",
        )
        with self.assertRaises(ServiceError):
            service.sign_followup("f-1", "ph-expired")  # 签署时点资格已失效

        service.register_staff(
            "ph-other-store", "pharmacist",
            qualifications=["licensed_pharmacist"],
            store_ids=["store-c"],
        )
        with self.assertRaises(ServiceError):
            service.sign_followup("f-1", "ph-other-store")  # 未获当前门店授权

        done = service.sign_followup("f-1", "ph-1")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["signed_by"], "ph-1")
        self.assertEqual(done["signed_at"], T0)

    def test_签署完成额度(self):
        service = make_full()
        service.schedule_followup("ag-1", "f-1", "reminder", T1)
        self.assertEqual(service.quota_balance("ag-1")["held_open"], 1)
        service.sign_followup("f-1", "ph-1")
        balance = service.quota_balance("ag-1")
        self.assertEqual(balance["held_open"], 0)
        self.assertEqual(balance["completed"], 1)
        self.assertEqual(balance["available"], 4)


class 同意范围测试(unittest.TestCase):
    def test_超出同意范围的回访被拒绝(self):
        service = make_full()
        with self.assertRaises(ServiceError):
            service.schedule_followup("ag-1", "f-1", "genetic_test", T1)

    def test_撤回同意冻结未来动作且保留历史(self):
        service = make_full()
        service.schedule_followup("ag-1", "f-1", "reminder", T1)
        result = service.withdraw_consent("ag-1")
        self.assertEqual(result["state"], "frozen")
        self.assertEqual(result["frozen_followups"], ["f-1"])
        # 预占额度已退回
        self.assertEqual(service.quota_balance("ag-1")["held_open"], 0)
        self.assertEqual(service.quota_balance("ag-1")["refunded"], 1)
        # 冻结后不能再预约或签署
        with self.assertRaises(ServiceError):
            service.schedule_followup("ag-1", "f-2", "reminder", T1)
        with self.assertRaises(ServiceError):
            service.sign_followup("f-1", "ph-1")
        # 历史记录依法保留：流水与审计仍可查
        compliance = service.view("ag-1", "compliance")
        self.assertTrue(compliance["ledger"])
        actions = [entry["action"] for entry in compliance["audit_trail"]]
        self.assertIn("withdraw_consent", actions)
        self.assertIn("schedule_followup", actions)


class 药物清单版本测试(unittest.TestCase):
    def test_新处方冻结依赖旧版本的未来回访(self):
        service = make_full()
        service.schedule_followup("ag-1", "f-1", "med_review", T1)
        result = service.record_medication_list(
            "ag-1", [{"name": "二甲双胍", "dose": "500mg"}]
        )
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["frozen_followups"], ["f-1"])
        # 冻结的回访退回预占额度，且不能签署
        self.assertEqual(service.quota_balance("ag-1")["held_open"], 0)
        with self.assertRaises(ServiceError):
            service.sign_followup("f-1", "ph-1")
        # 重排后基于新版本并重新预占
        replanned = service.replan_followup("f-1", T2)
        self.assertEqual(replanned["status"], "scheduled")
        self.assertEqual(replanned["based_on_version"], 2)
        self.assertEqual(service.quota_balance("ag-1")["held_open"], 1)
        self.assertEqual(service.sign_followup("f-1", "ph-1")["status"], "done")

    def test_系统只记录处方不改写内容(self):
        service = make_full()
        items_v2 = [{"name": "二甲双胍", "dose": "500mg"}, {"name": "阿司匹林", "dose": "100mg"}]
        service.record_medication_list("ag-1", items_v2)
        lists = service.store.list_medication_lists("ag-1")
        self.assertEqual([lst.version for lst in lists], [1, 2])
        self.assertEqual(lists[0].status, "superseded")
        self.assertEqual(list(lists[0].items), [{"name": "阿司匹林", "dose": "100mg"}])
        self.assertEqual(list(lists[1].items), items_v2)


class 额度事务测试(unittest.TestCase):
    def test_预占完成退回保持一致(self):
        service = make_full()
        service.payment_callback("ag-1", 1, request_key="pay-2")  # 共 6 次额度
        for index in range(6):
            service.schedule_followup("ag-1", f"f-{index}", "reminder", T1)
        with self.assertRaises(ServiceError):
            service.schedule_followup("ag-1", "f-6", "reminder", T1)  # 额度不足
        service.cancel_followup("f-0", "患者要求")
        service.sign_followup("f-1", "ph-1")
        balance = service.quota_balance("ag-1")
        self.assertEqual(balance["purchased"], 6)
        self.assertEqual(balance["held_open"], 4)
        self.assertEqual(balance["completed"], 1)
        self.assertEqual(balance["refunded"], 1)
        self.assertEqual(balance["available"], 1)

    def test_重复支付回调按业务键去重(self):
        service = make_full()
        again = service.payment_callback("ag-1", 5, request_key="pay-1")
        self.assertTrue(again["duplicate"])
        self.assertEqual(service.quota_balance("ag-1")["purchased"], 5)

    def test_同键不同内容进入复核且不生效(self):
        service = make_full()
        conflict = service.payment_callback("ag-1", 99, request_key="pay-1")
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(service.quota_balance("ag-1")["purchased"], 5)
        review_id = conflict["review_id"]
        with self.assertRaises(ServiceError):
            service.resolve_review(review_id, "sales-1", "通过")  # 销售无权复核
        resolved = service.resolve_review(review_id, "co-1", "以首次回调为准")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(service.quota_balance("ag-1")["purchased"], 5)

    def test_离线补录签署去重(self):
        service = make_full()
        service.schedule_followup("ag-1", "f-1", "reminder", T1)
        first = service.sign_followup("f-1", "ph-1", request_key="offline-f1")
        self.assertEqual(first["status"], "done")
        again = service.sign_followup("f-1", "ph-1", request_key="offline-f1")
        self.assertTrue(again["duplicate"])
        self.assertEqual(service.quota_balance("ag-1")["completed"], 1)


class 跨店交接测试(unittest.TestCase):
    def test_转店后原门店不能继续消费额度(self):
        service = make_full()
        service.schedule_followup("ag-1", "f-1", "reminder", T1)
        result = service.transfer_agreement("ag-1", "store-b")
        self.assertEqual(result["from_store"], "store-a")
        self.assertEqual(result["cancelled_followups"], ["f-1"])
        # 原门店的预占已退回
        self.assertEqual(service.quota_balance("ag-1")["refunded"], 1)
        # 新回访挂在新门店
        service.schedule_followup("ag-1", "f-2", "reminder", T1)
        followup = service.store.get_followup("f-2")
        self.assertEqual(followup.store_id, "store-b")
        holds = [
            e for e in service.store.list_ledger("ag-1")
            if e.kind == "hold" and e.ref_id == "f-2"
        ]
        self.assertEqual(holds[0].store_id, "store-b")
        # 只获原门店授权的药师不能签署新门店的回访
        service.register_staff(
            "ph-a-only", "pharmacist",
            qualifications=["licensed_pharmacist"], store_ids=["store-a"],
        )
        with self.assertRaises(ServiceError):
            service.sign_followup("f-2", "ph-a-only")
        self.assertEqual(service.sign_followup("f-2", "ph-1")["status"], "done")


class 异常升级测试(unittest.TestCase):
    def test_销售人员不能关闭安全异常(self):
        service = make_full()
        service.open_escalation("ag-1", "esc-1", "safety", "sales-1", "疑似不良反应")
        with self.assertRaises(ServiceError):
            service.close_escalation("esc-1", "sales-1")
        closed = service.close_escalation("esc-1", "ph-1")
        self.assertEqual(closed["status"], "closed")

    def test_非安全异常由药师或合规关闭(self):
        service = make_full()
        service.open_escalation("ag-1", "esc-1", "service", "sales-1", "回访超时未联系")
        with self.assertRaises(ServiceError):
            service.close_escalation("esc-1", "sales-1")
        self.assertEqual(service.close_escalation("esc-1", "co-1")["status"], "closed")


class 角色视图测试(unittest.TestCase):
    def test_三种角色各见其所当见(self):
        service = make_full()
        service.add_goal("ag-1", "血压控制在 130/80 以下")
        service.schedule_followup("ag-1", "f-1", "reminder", T1)
        service.open_escalation("ag-1", "esc-1", "safety", "sales-1", "疑似不良反应")

        patient = service.view("ag-1", "patient")
        self.assertEqual(patient["consent"]["status"], "granted")
        self.assertEqual(patient["goals"], ["血压控制在 130/80 以下"])
        self.assertEqual([p["followup_id"] for p in patient["plan"]], ["f-1"])
        self.assertNotIn("ledger", patient)
        self.assertNotIn("open_escalations", patient)

        pharmacist = service.view("ag-1", "pharmacist")
        self.assertEqual(pharmacist["medication_list"]["version"], 1)
        self.assertEqual(pharmacist["followups"][0]["based_on_version"], 1)
        self.assertEqual(pharmacist["open_escalations"][0]["escalation_id"], "esc-1")

        compliance = service.view("ag-1", "compliance")
        self.assertEqual(compliance["open_escalations"][0]["kind"], "safety")
        self.assertTrue(compliance["ledger"])
        self.assertEqual(compliance["quota"]["available"], 4)
        self.assertTrue(compliance["audit_trail"])

        with self.assertRaises(ServiceError):
            service.view("ag-1", "stranger")


class 重启恢复测试(unittest.TestCase):
    def test_重启后恢复逾期回访与升级队列(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "care.db"
            service = make_service(T0, Store(path))
            service.register_staff(
                "ph-1", "pharmacist",
                qualifications=["licensed_pharmacist"], store_ids=["store-a"],
            )
            service.register_staff("sales-1", "sales", store_ids=["store-a"])
            service.open_agreement("ag-1", "pat-1", "store-a", ["reminder"])
            service.payment_callback("ag-1", 2, request_key="pay-1")
            service.schedule_followup("ag-1", "f-1", "reminder", T1)
            service.open_escalation("ag-1", "esc-1", "safety", "sales-1", "疑似不良反应")

            # 时间推进到回访过期之后，用同一数据库文件重建服务（模拟重启）
            restarted = Service(Store(path), FixedClock(T2))
            self.assertEqual(restarted.store.get_followup("f-1").status, "overdue")
            compliance = restarted.view("ag-1", "compliance")
            self.assertEqual(compliance["overdue_followups"], ["f-1"])
            self.assertEqual(
                [e["escalation_id"] for e in compliance["open_escalations"]], ["esc-1"]
            )
            # 逾期回访仍可由合格药师补签
            self.assertEqual(restarted.sign_followup("f-1", "ph-1")["status"], "done")


class 接口测试(unittest.TestCase):
    def test_业务拒绝以错误对象返回(self):
        service = make_full()
        raw = handle(json.dumps({
            "action": "sign_followup", "followup_id": "nope", "staff_id": "ph-1",
        }), service)
        self.assertIn("error", json.loads(raw))

    def test_完整流程经接口走通(self):
        service = make_service()
        handle(json.dumps({
            "action": "register_staff", "staff_id": "ph-1", "role": "pharmacist",
            "qualifications": ["licensed_pharmacist"], "store_ids": ["store-a"],
        }), service)
        handle(json.dumps({
            "action": "open_agreement", "agreement_id": "ag-1", "patient_id": "pat-1",
            "store_id": "store-a", "consent_scope": ["reminder"],
        }), service)
        handle(json.dumps({
            "action": "payment_callback", "agreement_id": "ag-1",
            "amount": 1, "request_key": "pay-1",
        }), service)
        handle(json.dumps({
            "action": "schedule_followup", "agreement_id": "ag-1",
            "followup_id": "f-1", "kind": "reminder", "scheduled_at": T1,
        }), service)
        raw = handle(json.dumps({
            "action": "sign_followup", "followup_id": "f-1", "staff_id": "ph-1",
        }), service)
        self.assertEqual(json.loads(raw)["status"], "done")
        view = json.loads(handle(json.dumps({
            "action": "view", "agreement_id": "ag-1", "role": "compliance",
        }), service))
        self.assertEqual(view["quota"]["completed"], 1)


if __name__ == "__main__":
    unittest.main()
