"""连续照护协议的应用服务。

系统只登记、核对与冻结信息，不做诊断，也不修改医嘱：药物清单只能
以新版本形式按处方来源追加，历史版本与历史动作全部保留，作为执行
依据与依法需要的历史记录。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from .clock import Clock
from .domain import (
    CONSENT_GRANTED,
    CONSENT_WITHDRAWN,
    ESCALATION_CLOSED,
    ESCALATION_OPEN,
    FOLLOWUP_DONE,
    FOLLOWUP_FROZEN,
    FOLLOWUP_SCHEDULED,
    PROTOCOL_ACTIVE,
    PROTOCOL_FROZEN,
    QUOTA_CONSUME,
    QUOTA_HOLD,
    QUOTA_RELEASE,
    REVIEW_OPEN,
    ROLE_PHARMACIST,
    ROLE_SALES,
    ConflictError,
    DomainError,
    NotFoundError,
    Record,
)
from .store import Store


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment)


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class Service:
    def __init__(self, store: Store | None = None, clock: Clock | None = None) -> None:
        self.store = store or Store()
        self.clock = clock or Clock()

    # ---- 基础行为 ----
    def health(self) -> dict[str, str]:
        return {"service": "pharmacy_care", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str | int]:
        record = Record(record_id, owner_id, "draft", 1, self.clock.now())
        self.store.add(record)
        return record.__dict__.copy()

    def find(self, record_id: str) -> dict[str, str | int] | None:
        record = self.store.get(record_id)
        return record.__dict__.copy() if record else None

    # ---- 查询辅助 ----
    def _one(self, sql: str, args: tuple) -> dict | None:
        row = self.store.connection.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: tuple) -> list[dict]:
        return [dict(row) for row in self.store.connection.execute(sql, args).fetchall()]

    def _require_protocol(self, conn, protocol_id: str) -> dict:
        row = conn.execute(
            "SELECT * FROM protocols WHERE protocol_id=?", (protocol_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"协议不存在: {protocol_id}")
        return dict(row)

    def _require_practitioner(self, conn, practitioner_id: str) -> dict:
        row = conn.execute(
            "SELECT * FROM practitioners WHERE practitioner_id=?", (practitioner_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"人员不存在: {practitioner_id}")
        return dict(row)

    def _latest_consent(self, conn, protocol_id: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM consents WHERE protocol_id=? ORDER BY version DESC LIMIT 1",
            (protocol_id,),
        ).fetchone()
        return dict(row) if row else None

    def _require_active_consent(self, conn, protocol_id: str) -> dict:
        consent = self._latest_consent(conn, protocol_id)
        if not consent or consent["status"] != CONSENT_GRANTED:
            raise DomainError("患者同意不存在或已撤回")
        return consent

    def _check_pharmacist(self, practitioner: dict, moment: str) -> None:
        """一次回访只能由当时具备资格的药师签署。"""
        if practitioner["role"] != ROLE_PHARMACIST:
            raise DomainError("只有药师能执行该专业动作")
        if not practitioner["active"]:
            raise DomainError("药师资格已停用")
        valid_from = _parse(practitioner["valid_from"])
        valid_until = _parse(practitioner["valid_until"])
        if not (valid_from <= _parse(moment) <= valid_until):
            raise DomainError("药师在当时不具备执业资格")

    # ---- 协议与同意 ----
    def create_protocol(
        self, protocol_id: str, patient_id: str, store_id: str, quota_total: int = 0
    ) -> dict:
        now = self.clock.now()
        with self.store.connection as conn:
            conn.execute(
                "INSERT INTO protocols(protocol_id,patient_id,store_id,state,medication_version,quota_total,created_at,updated_at)"
                " VALUES(?,?,?,?,0,?,?,?)",
                (protocol_id, patient_id, store_id, PROTOCOL_ACTIVE, quota_total, now, now),
            )
        return self.get_protocol(protocol_id)

    def get_protocol(self, protocol_id: str) -> dict:
        return self._require_protocol(self.store.connection, protocol_id)

    def grant_consent(self, consent_id: str, protocol_id: str, scope: list[str]) -> dict:
        now = self.clock.now()
        with self.store.connection as conn:
            protocol = self._require_protocol(conn, protocol_id)
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS v FROM consents WHERE protocol_id=?",
                (protocol_id,),
            ).fetchone()
            version = row["v"]
            conn.execute(
                "INSERT INTO consents(consent_id,protocol_id,version,scope_json,status,granted_at)"
                " VALUES(?,?,?,?,?,?)",
                (consent_id, protocol_id, version, _canonical(scope), CONSENT_GRANTED, now),
            )
            # 重新授予同意后协议恢复活动；此前被冻结的回访保持冻结，
            # 因为它们依赖的是旧信息。
            if protocol["state"] == PROTOCOL_FROZEN:
                conn.execute(
                    "UPDATE protocols SET state=?, updated_at=? WHERE protocol_id=?",
                    (PROTOCOL_ACTIVE, now, protocol_id),
                )
        return self._one("SELECT * FROM consents WHERE consent_id=?", (consent_id,))

    def withdraw_consent(self, protocol_id: str) -> dict:
        """撤回同意：冻结依赖旧信息的未来动作，历史记录全部保留。"""
        now = self.clock.now()
        with self.store.connection as conn:
            self._require_protocol(conn, protocol_id)
            consent = self._latest_consent(conn, protocol_id)
            if not consent or consent["status"] != CONSENT_GRANTED:
                raise DomainError("没有生效中的患者同意")
            conn.execute(
                "UPDATE consents SET status=?, withdrawn_at=? WHERE consent_id=?",
                (CONSENT_WITHDRAWN, now, consent["consent_id"]),
            )
            conn.execute(
                "UPDATE protocols SET state=?, updated_at=? WHERE protocol_id=?",
                (PROTOCOL_FROZEN, now, protocol_id),
            )
            conn.execute(
                "UPDATE followups SET status=? WHERE protocol_id=? AND status=?",
                (FOLLOWUP_FROZEN, protocol_id, FOLLOWUP_SCHEDULED),
            )
        return self.get_protocol(protocol_id)

    # ---- 人员与授权 ----
    def register_practitioner(
        self,
        practitioner_id: str,
        name: str,
        role: str,
        qualification: str,
        store_id: str,
        valid_from: str,
        valid_until: str,
    ) -> dict:
        with self.store.connection as conn:
            conn.execute(
                "INSERT INTO practitioners(practitioner_id,name,role,qualification,store_id,valid_from,valid_until,active)"
                " VALUES(?,?,?,?,?,?,?,1)",
                (practitioner_id, name, role, qualification, store_id, valid_from, valid_until),
            )
        return self._one(
            "SELECT * FROM practitioners WHERE practitioner_id=?", (practitioner_id,)
        )

    # ---- 药物清单版本 ----
    def add_medication_list(
        self, protocol_id: str, items: list[dict], source: str = "prescription"
    ) -> dict:
        """按处方追加清单新版本；旧版本保留，依赖旧版本的未来回访被冻结。"""
        now = self.clock.now()
        with self.store.connection as conn:
            protocol = self._require_protocol(conn, protocol_id)
            version = protocol["medication_version"] + 1
            conn.execute(
                "UPDATE medication_lists SET status='superseded' WHERE protocol_id=? AND status='current'",
                (protocol_id,),
            )
            conn.execute(
                "INSERT INTO medication_lists(protocol_id,version,items_json,source,status,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (protocol_id, version, _canonical(items), source, "current", now),
            )
            conn.execute(
                "UPDATE protocols SET medication_version=?, updated_at=? WHERE protocol_id=?",
                (version, now, protocol_id),
            )
            conn.execute(
                "UPDATE followups SET status=? WHERE protocol_id=? AND status=? AND medication_version<?",
                (FOLLOWUP_FROZEN, protocol_id, FOLLOWUP_SCHEDULED, version),
            )
        return {
            "protocol_id": protocol_id,
            "version": version,
            "items": items,
            "source": source,
            "status": "current",
        }

    # ---- 服务目标 ----
    def add_goal(self, goal_id: str, protocol_id: str, description: str) -> dict:
        with self.store.connection as conn:
            self._require_protocol(conn, protocol_id)
            conn.execute(
                "INSERT INTO goals(goal_id,protocol_id,description,status) VALUES(?,?,?,'open')",
                (goal_id, protocol_id, description),
            )
        return self._one("SELECT * FROM goals WHERE goal_id=?", (goal_id,))

    # ---- 回访事件 ----
    def schedule_followup(
        self, followup_id: str, protocol_id: str, scheduled_at: str, business_key: str
    ) -> dict:
        existing = self._one(
            "SELECT * FROM followups WHERE business_key=?", (business_key,)
        )
        if existing:
            if (
                existing["protocol_id"] == protocol_id
                and existing["scheduled_at"] == scheduled_at
                and existing["status"] == FOLLOWUP_SCHEDULED
            ):
                return existing  # 幂等重放
            review_id = self._record_review(
                business_key,
                "followup",
                "回访业务键内容冲突",
                {"protocol_id": protocol_id, "scheduled_at": scheduled_at},
            )
            raise ConflictError(f"回访业务键 {business_key} 内容冲突，已进入复核 {review_id}")
        with self.store.connection as conn:
            protocol = self._require_protocol(conn, protocol_id)
            if protocol["state"] != PROTOCOL_ACTIVE:
                raise DomainError("协议已冻结，不能安排新的回访")
            self._require_active_consent(conn, protocol_id)
            conn.execute(
                "INSERT INTO followups(followup_id,protocol_id,business_key,scheduled_at,status,medication_version)"
                " VALUES(?,?,?,?,?,?)",
                (
                    followup_id,
                    protocol_id,
                    business_key,
                    scheduled_at,
                    FOLLOWUP_SCHEDULED,
                    protocol["medication_version"],
                ),
            )
        return self._one("SELECT * FROM followups WHERE followup_id=?", (followup_id,))

    def complete_followup(
        self,
        followup_id: str,
        pharmacist_id: str,
        occurred_at: str | None = None,
        note: str = "",
    ) -> dict:
        """签署回访并同事务结算额度；任何一步失败整体回滚。"""
        now = self.clock.now()
        occurred = occurred_at or now
        with self.store.connection as conn:
            row = conn.execute(
                "SELECT * FROM followups WHERE followup_id=?", (followup_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"回访不存在: {followup_id}")
            followup = dict(row)
            if followup["status"] != FOLLOWUP_SCHEDULED:
                raise DomainError("回访不在可执行状态")
            protocol = self._require_protocol(conn, followup["protocol_id"])
            if protocol["state"] != PROTOCOL_ACTIVE:
                raise DomainError("协议已冻结，不能执行回访")
            self._require_active_consent(conn, protocol["protocol_id"])
            pharmacist = self._require_practitioner(conn, pharmacist_id)
            self._check_pharmacist(pharmacist, occurred)
            if pharmacist["store_id"] != protocol["store_id"]:
                raise DomainError("药师未在协议当前门店获得授权")
            self._consume_quota(conn, protocol, followup_id, f"回访 {followup_id} 完成")
            conn.execute(
                "UPDATE followups SET status=?, signed_by=?, signed_at=?, occurred_at=?, note=?"
                " WHERE followup_id=?",
                (FOLLOWUP_DONE, pharmacist_id, now, occurred, note, followup_id),
            )
        return self._one("SELECT * FROM followups WHERE followup_id=?", (followup_id,))

    def backfill_followup(
        self,
        followup_id: str,
        business_key: str,
        protocol_id: str,
        pharmacist_id: str,
        occurred_at: str,
        note: str = "",
    ) -> dict:
        """离线补录：按业务键去重，内容冲突进入复核，历史记录不覆盖。"""
        existing = self._one(
            "SELECT * FROM followups WHERE business_key=?", (business_key,)
        )
        if existing:
            same = (
                existing["protocol_id"] == protocol_id
                and existing["occurred_at"] == occurred_at
                and existing["signed_by"] == pharmacist_id
                and (existing["note"] or "") == note
            )
            if same:
                return existing  # 幂等重放
            review_id = self._record_review(
                business_key,
                "backfill",
                "离线补录内容冲突",
                {
                    "protocol_id": protocol_id,
                    "occurred_at": occurred_at,
                    "pharmacist_id": pharmacist_id,
                    "note": note,
                },
            )
            raise ConflictError(f"补录业务键 {business_key} 内容冲突，已进入复核 {review_id}")
        now = self.clock.now()
        with self.store.connection as conn:
            protocol = self._require_protocol(conn, protocol_id)
            pharmacist = self._require_practitioner(conn, pharmacist_id)
            self._check_pharmacist(pharmacist, occurred_at)
            self._consume_quota(conn, protocol, followup_id, f"回访 {followup_id} 补录")
            conn.execute(
                "INSERT INTO followups(followup_id,protocol_id,business_key,scheduled_at,status,"
                "medication_version,signed_by,signed_at,occurred_at,note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    followup_id,
                    protocol_id,
                    business_key,
                    occurred_at,
                    FOLLOWUP_DONE,
                    protocol["medication_version"],
                    pharmacist_id,
                    now,
                    occurred_at,
                    note,
                ),
            )
        return self._one("SELECT * FROM followups WHERE followup_id=?", (followup_id,))

    # ---- 异常升级 ----
    def open_escalation(
        self, escalation_id: str, protocol_id: str, kind: str, detail: str
    ) -> dict:
        now = self.clock.now()
        with self.store.connection as conn:
            self._require_protocol(conn, protocol_id)
            conn.execute(
                "INSERT INTO escalations(escalation_id,protocol_id,kind,detail,status,opened_at)"
                " VALUES(?,?,?,?,?,?)",
                (escalation_id, protocol_id, kind, detail, ESCALATION_OPEN, now),
            )
        return self._one("SELECT * FROM escalations WHERE escalation_id=?", (escalation_id,))

    def close_escalation(self, escalation_id: str, actor_id: str) -> dict:
        now = self.clock.now()
        with self.store.connection as conn:
            row = conn.execute(
                "SELECT * FROM escalations WHERE escalation_id=?", (escalation_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"异常不存在: {escalation_id}")
            escalation = dict(row)
            if escalation["status"] != ESCALATION_OPEN:
                raise DomainError("异常已关闭")
            actor = self._require_practitioner(conn, actor_id)
            if actor["role"] == ROLE_SALES:
                raise DomainError("销售人员不能关闭安全异常")
            self._check_pharmacist(actor, now)
            conn.execute(
                "UPDATE escalations SET status=?, closed_by=?, closed_at=? WHERE escalation_id=?",
                (ESCALATION_CLOSED, actor_id, now, escalation_id),
            )
        return self._one("SELECT * FROM escalations WHERE escalation_id=?", (escalation_id,))

    # ---- 服务额度（预占/完成/退回在同一事务语义下） ----
    def _active_holds(self, conn, protocol_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM quota_ledger WHERE protocol_id=? AND kind=? AND entry_id NOT IN ("
            " SELECT ref_id FROM quota_ledger WHERE protocol_id=? AND kind IN (?,?)"
            " AND ref_id IS NOT NULL)",
            (protocol_id, QUOTA_HOLD, protocol_id, QUOTA_CONSUME, QUOTA_RELEASE),
        ).fetchall()
        return [dict(row) for row in rows]

    def _quota_available(self, conn, protocol: dict) -> int:
        protocol_id = protocol["protocol_id"]
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS c FROM quota_ledger WHERE protocol_id=? AND kind=?",
            (protocol_id, QUOTA_CONSUME),
        ).fetchone()
        consumed = row["c"]
        held = sum(hold["amount"] for hold in self._active_holds(conn, protocol_id))
        return protocol["quota_total"] - consumed - held

    def _insert_ledger(
        self, conn, protocol: dict, kind: str, amount: int, ref_id: str | None, note: str
    ) -> str:
        entry_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO quota_ledger(entry_id,protocol_id,store_id,kind,amount,ref_id,note,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                entry_id,
                protocol["protocol_id"],
                protocol["store_id"],
                kind,
                amount,
                ref_id,
                note,
                self.clock.now(),
            ),
        )
        return entry_id

    def _consume_quota(self, conn, protocol: dict, followup_id: str, note: str) -> None:
        hold = next(
            (h for h in self._active_holds(conn, protocol["protocol_id"]) if h["ref_id"] == followup_id),
            None,
        )
        if hold:
            self._insert_ledger(conn, protocol, QUOTA_CONSUME, hold["amount"], hold["entry_id"], note)
            return
        if self._quota_available(conn, protocol) < 1:
            raise DomainError("服务额度不足")
        self._insert_ledger(conn, protocol, QUOTA_CONSUME, 1, None, note)

    def hold_quota(
        self, protocol_id: str, amount: int, ref_id: str | None = None, note: str = ""
    ) -> dict:
        with self.store.connection as conn:
            protocol = self._require_protocol(conn, protocol_id)
            if amount <= 0:
                raise DomainError("预占额度必须为正数")
            if self._quota_available(conn, protocol) < amount:
                raise DomainError("服务额度不足，无法预占")
            entry_id = self._insert_ledger(conn, protocol, QUOTA_HOLD, amount, ref_id, note)
        return self._one("SELECT * FROM quota_ledger WHERE entry_id=?", (entry_id,))

    def release_quota(self, hold_entry_id: str, note: str = "") -> dict:
        with self.store.connection as conn:
            row = conn.execute(
                "SELECT * FROM quota_ledger WHERE entry_id=?", (hold_entry_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"额度流水不存在: {hold_entry_id}")
            hold = dict(row)
            if hold["kind"] != QUOTA_HOLD:
                raise DomainError("只能退回预占流水")
            protocol = self._require_protocol(conn, hold["protocol_id"])
            active_ids = {h["entry_id"] for h in self._active_holds(conn, protocol["protocol_id"])}
            if hold_entry_id not in active_ids:
                raise DomainError("该预占已结算或已退回")
            entry_id = self._insert_ledger(
                conn, protocol, QUOTA_RELEASE, hold["amount"], hold_entry_id, note or "退回预占"
            )
        return self._one("SELECT * FROM quota_ledger WHERE entry_id=?", (entry_id,))

    # ---- 跨店交接 ----
    def transfer_store(self, handoff_id: str, protocol_id: str, to_store: str) -> dict:
        """转店：同一事务内释放原门店全部有效预占并变更归属门店。"""
        now = self.clock.now()
        with self.store.connection as conn:
            protocol = self._require_protocol(conn, protocol_id)
            if protocol["state"] != PROTOCOL_ACTIVE:
                raise DomainError("冻结的协议不能转店")
            from_store = protocol["store_id"]
            if from_store == to_store:
                raise DomainError("协议已在该门店")
            for hold in self._active_holds(conn, protocol_id):
                self._insert_ledger(
                    conn, protocol, QUOTA_RELEASE, hold["amount"], hold["entry_id"], "转店释放预占"
                )
            conn.execute(
                "INSERT INTO handoffs(handoff_id,protocol_id,from_store,to_store,transferred_at)"
                " VALUES(?,?,?,?,?)",
                (handoff_id, protocol_id, from_store, to_store, now),
            )
            conn.execute(
                "UPDATE protocols SET store_id=?, updated_at=? WHERE protocol_id=?",
                (to_store, now, protocol_id),
            )
        return self._one("SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,))

    # ---- 支付回调 ----
    def record_payment(
        self, payment_id: str, protocol_id: str, business_key: str, amount: int, payload: dict
    ) -> dict:
        """支付回调按业务键去重；内容冲突进入复核，不覆盖原记录。"""
        canonical = _canonical(payload)
        existing = self._one("SELECT * FROM payments WHERE business_key=?", (business_key,))
        if existing:
            if existing["payload_json"] == canonical and existing["amount"] == amount:
                return existing  # 幂等重放
            review_id = self._record_review(business_key, "payment", "支付回调内容冲突", payload)
            raise ConflictError(f"支付业务键 {business_key} 内容冲突，已进入复核 {review_id}")
        with self.store.connection as conn:
            self._require_protocol(conn, protocol_id)
            conn.execute(
                "INSERT INTO payments(payment_id,protocol_id,business_key,amount,payload_json,received_at)"
                " VALUES(?,?,?,?,?,?)",
                (payment_id, protocol_id, business_key, amount, canonical, self.clock.now()),
            )
        return self._one("SELECT * FROM payments WHERE payment_id=?", (payment_id,))

    # ---- 复核 ----
    def _record_review(self, business_key: str, category: str, reason: str, payload: dict) -> str:
        review_id = uuid.uuid4().hex
        with self.store.connection as conn:
            conn.execute(
                "INSERT INTO reviews(review_id,business_key,category,reason,payload_json,status,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (review_id, business_key, category, reason, _canonical(payload), REVIEW_OPEN, self.clock.now()),
            )
        return review_id

    # ---- 分角色视图 ----
    def patient_view(self, protocol_id: str) -> dict:
        conn = self.store.connection
        protocol = self._require_protocol(conn, protocol_id)
        consent = self._latest_consent(conn, protocol_id)
        goals = self._all(
            "SELECT goal_id, description, status FROM goals WHERE protocol_id=?", (protocol_id,)
        )
        followups = self._all(
            "SELECT followup_id, scheduled_at, status, occurred_at FROM followups"
            " WHERE protocol_id=? ORDER BY scheduled_at",
            (protocol_id,),
        )
        return {
            "protocol_id": protocol_id,
            "patient_id": protocol["patient_id"],
            "state": protocol["state"],
            "consent": (
                {"status": consent["status"], "scope": json.loads(consent["scope_json"])}
                if consent
                else None
            ),
            "medication_version": protocol["medication_version"],
            "goals": goals,
            "followups": followups,
        }

    def pharmacist_view(self, protocol_id: str) -> dict:
        conn = self.store.connection
        protocol = self._require_protocol(conn, protocol_id)
        view = self.patient_view(protocol_id)
        view["followups"] = self._all(
            "SELECT * FROM followups WHERE protocol_id=? ORDER BY scheduled_at", (protocol_id,)
        )
        view["medication_lists"] = [
            {
                "version": row["version"],
                "items": json.loads(row["items_json"]),
                "source": row["source"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in self._all(
                "SELECT version, items_json, source, status, created_at FROM medication_lists"
                " WHERE protocol_id=? ORDER BY version",
                (protocol_id,),
            )
        ]
        view["open_escalations"] = self._all(
            "SELECT * FROM escalations WHERE protocol_id=? AND status=?",
            (protocol_id, ESCALATION_OPEN),
        )
        view["quota_available"] = self._quota_available(conn, protocol)
        return view

    def compliance_view(self, protocol_id: str) -> dict:
        view = self.pharmacist_view(protocol_id)
        view["consent_history"] = [
            {
                "consent_id": row["consent_id"],
                "version": row["version"],
                "scope": json.loads(row["scope_json"]),
                "status": row["status"],
                "granted_at": row["granted_at"],
                "withdrawn_at": row["withdrawn_at"],
            }
            for row in self._all(
                "SELECT consent_id, version, scope_json, status, granted_at, withdrawn_at"
                " FROM consents WHERE protocol_id=? ORDER BY version",
                (protocol_id,),
            )
        ]
        view["escalations"] = self._all(
            "SELECT * FROM escalations WHERE protocol_id=? ORDER BY opened_at", (protocol_id,)
        )
        view["quota_ledger"] = self._all(
            "SELECT * FROM quota_ledger WHERE protocol_id=? ORDER BY created_at", (protocol_id,)
        )
        view["payments"] = self._all(
            "SELECT * FROM payments WHERE protocol_id=? ORDER BY received_at", (protocol_id,)
        )
        view["handoffs"] = self._all(
            "SELECT * FROM handoffs WHERE protocol_id=? ORDER BY transferred_at", (protocol_id,)
        )
        view["reviews"] = self._all("SELECT * FROM reviews ORDER BY created_at", ())
        return view

    # ---- 重启恢复 ----
    def recover(self) -> dict:
        """恢复逾期回访与未关闭的升级队列；数据在 SQLite 中持久化，重启后可用。"""
        now = self.clock.now()
        return {
            "overdue_followups": self._all(
                "SELECT * FROM followups WHERE status=? AND scheduled_at<? ORDER BY scheduled_at",
                (FOLLOWUP_SCHEDULED, now),
            ),
            "open_escalations": self._all(
                "SELECT * FROM escalations WHERE status=? ORDER BY opened_at",
                (ESCALATION_OPEN,),
            ),
        }
