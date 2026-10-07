"""连续照护协议的应用服务：同意范围、清单版本、回访签署、异常升级、额度事务与跨店交接。

系统只记录处方与执行事实，不做诊断、不改写医嘱；
患者撤回同意或收到新处方时，冻结依赖旧信息的未来动作，历史记录依法保留。
"""
import hashlib
import json
from datetime import datetime, timezone

from .clock import Clock
from .domain import (
    AGREEMENT_ACTIVE,
    AGREEMENT_FROZEN,
    CONSENT_GRANTED,
    CONSENT_WITHDRAWN,
    ESCALATION_OPEN,
    ESCALATION_SAFETY,
    FOLLOWUP_CANCELLED,
    FOLLOWUP_DONE,
    FOLLOWUP_FROZEN,
    FOLLOWUP_OVERDUE,
    FOLLOWUP_SCHEDULED,
    LEDGER_COMPLETE,
    LEDGER_HOLD,
    LEDGER_PURCHASE,
    LEDGER_REFUND,
    LIST_CURRENT,
    PHARMACIST_QUALIFICATION,
    REVIEW_OPEN,
    ROLE_COMPLIANCE,
    ROLE_PATIENT,
    ROLE_PHARMACIST,
    STAFF_ROLES,
    Agreement,
    Escalation,
    FollowUp,
    Goal,
    MedicationList,
    Record,
    Staff,
)
from .store import Store

_FAR_PAST = "0001-01-01T00:00:00+00:00"
_FAR_FUTURE = "9999-12-31T23:59:59+00:00"
_PENDING = (FOLLOWUP_SCHEDULED, FOLLOWUP_OVERDUE)


class ServiceError(Exception):
    """业务规则拒绝。"""


def _parse(moment: str) -> datetime:
    value = datetime.fromisoformat(moment)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Service:
    def __init__(self, store: Store | None = None, clock: Clock | None = None) -> None:
        self.store = store or Store()
        self.clock = clock or Clock()
        # 启动即恢复逾期回访与升级队列，重启后不丢失待办。
        self.recover()

    def _now(self) -> str:
        return self.clock.now()

    # ---- 基础登记（保留） ----
    def health(self) -> dict[str, str]:
        return {"service": "pharmacy_care", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str | int]:
        record = Record(record_id, owner_id, "draft", 1, self._now())
        self.store.add(record)
        return record.__dict__.copy()

    def find(self, record_id: str) -> dict[str, str | int] | None:
        record = self.store.get(record_id)
        return record.__dict__.copy() if record else None

    # ---- 人员与授权 ----
    def register_staff(
        self,
        staff_id: str,
        role: str,
        qualifications=(),
        store_ids=(),
        valid_from: str | None = None,
        valid_until: str | None = None,
    ) -> dict:
        if role not in STAFF_ROLES:
            raise ServiceError(f"未知的人员角色: {role}")
        if self.store.get_staff(staff_id):
            raise ServiceError("人员编号已存在")
        staff = Staff(
            staff_id=staff_id,
            role=role,
            qualifications=tuple(qualifications),
            store_ids=tuple(store_ids),
            valid_from=valid_from or _FAR_PAST,
            valid_until=valid_until or _FAR_FUTURE,
        )
        with self.store.transaction():
            self.store.add_staff(staff)
            self.store.add_audit(self._now(), staff_id, "register_staff", None, {"role": role})
        return {"staff_id": staff_id, "role": role, "store_ids": list(staff.store_ids)}

    # ---- 协议与同意 ----
    def open_agreement(
        self,
        agreement_id: str,
        patient_id: str,
        store_id: str,
        consent_scope,
        request_key: str | None = None,
    ) -> dict:
        payload = {
            "action": "open_agreement",
            "agreement_id": agreement_id,
            "patient_id": patient_id,
            "store_id": store_id,
            "consent_scope": list(consent_scope),
        }
        return self._idempotent(
            request_key,
            payload,
            lambda: self._open_agreement(agreement_id, patient_id, store_id, tuple(consent_scope)),
        )

    def _open_agreement(self, agreement_id, patient_id, store_id, consent_scope) -> dict:
        if self.store.get_agreement(agreement_id):
            raise ServiceError("协议编号已存在")
        agreement = Agreement(
            agreement_id=agreement_id,
            patient_id=patient_id,
            store_id=store_id,
            state=AGREEMENT_ACTIVE,
            consent_scope=consent_scope,
            consent_status=CONSENT_GRANTED,
            revision=1,
            created_at=self._now(),
        )
        with self.store.transaction():
            self.store.add_agreement(agreement)
            self.store.add_audit(
                self._now(), patient_id, "open_agreement", agreement_id,
                {"store_id": store_id, "consent_scope": list(consent_scope)},
            )
        return self._agreement_view(agreement)

    def withdraw_consent(self, agreement_id: str, request_key: str | None = None) -> dict:
        payload = {"action": "withdraw_consent", "agreement_id": agreement_id}
        return self._idempotent(request_key, payload, lambda: self._withdraw_consent(agreement_id))

    def _withdraw_consent(self, agreement_id: str) -> dict:
        agreement = self._must_agreement(agreement_id)
        if agreement.consent_status == CONSENT_WITHDRAWN:
            raise ServiceError("患者已撤回同意")
        now = self._now()
        with self.store.transaction():
            self.store.update_agreement(
                agreement_id,
                state=AGREEMENT_FROZEN,
                consent_status=CONSENT_WITHDRAWN,
                revision=agreement.revision + 1,
            )
            frozen = self._freeze_pending_followups(agreement, now, reason="consent_withdrawn")
            self.store.add_audit(
                now, agreement.patient_id, "withdraw_consent", agreement_id,
                {"frozen_followups": frozen},
            )
        return {"agreement_id": agreement_id, "state": AGREEMENT_FROZEN,
                "consent_status": CONSENT_WITHDRAWN, "frozen_followups": frozen}

    # ---- 药物清单版本（只记录处方，不做诊断、不改写医嘱） ----
    def record_medication_list(
        self,
        agreement_id: str,
        items,
        source: str = "prescription",
        request_key: str | None = None,
    ) -> dict:
        payload = {
            "action": "record_medication_list",
            "agreement_id": agreement_id,
            "items": list(items),
            "source": source,
        }
        return self._idempotent(
            request_key, payload,
            lambda: self._record_medication_list(agreement_id, tuple(items), source),
        )

    def _record_medication_list(self, agreement_id, items, source) -> dict:
        agreement = self._must_agreement(agreement_id)
        self._require_active(agreement)
        current = self.store.get_current_list(agreement_id)
        version = current.version + 1 if current else 1
        now = self._now()
        med_list = MedicationList(
            list_id=f"{agreement_id}-ml-{version}",
            agreement_id=agreement_id,
            version=version,
            items=items,
            source=source,
            status=LIST_CURRENT,
            created_at=now,
        )
        with self.store.transaction():
            self.store.supersede_current_list(agreement_id)
            self.store.add_medication_list(med_list)
            # 新处方到达：冻结仍依赖旧清单版本的未来回访，并退回其预占额度。
            frozen = self._freeze_pending_followups(
                agreement, now, reason="list_superseded", only_below_version=version,
            )
            self.store.add_audit(
                now, "system", "record_medication_list", agreement_id,
                {"version": version, "source": source, "frozen_followups": frozen},
            )
        return {"list_id": med_list.list_id, "version": version, "frozen_followups": frozen}

    # ---- 服务目标 ----
    def add_goal(self, agreement_id: str, text: str, goal_id: str | None = None) -> dict:
        agreement = self._must_agreement(agreement_id)
        self._require_active(agreement)
        goal_id = goal_id or f"goal-{agreement_id}-{len(self.store.list_goals(agreement_id)) + 1}"
        if self.store.get_agreement(goal_id):  # 防御性检查，正常不会命中
            raise ServiceError("目标编号冲突")
        goal = Goal(goal_id=goal_id, agreement_id=agreement_id, text=text, created_at=self._now())
        with self.store.transaction():
            self.store.add_goal(goal)
            self.store.add_audit(self._now(), "system", "add_goal", agreement_id, {"goal_id": goal_id})
        return {"goal_id": goal_id, "agreement_id": agreement_id, "text": text}

    # ---- 回访：预约预占额度，签署完成额度，取消/冻结退回额度，均在同一事务内 ----
    def schedule_followup(
        self,
        agreement_id: str,
        followup_id: str,
        kind: str,
        scheduled_at: str,
        request_key: str | None = None,
    ) -> dict:
        payload = {
            "action": "schedule_followup",
            "agreement_id": agreement_id,
            "followup_id": followup_id,
            "kind": kind,
            "scheduled_at": scheduled_at,
        }
        return self._idempotent(
            request_key, payload,
            lambda: self._schedule_followup(agreement_id, followup_id, kind, scheduled_at),
        )

    def _schedule_followup(self, agreement_id, followup_id, kind, scheduled_at) -> dict:
        agreement = self._must_agreement(agreement_id)
        self._require_active(agreement)
        if kind not in agreement.consent_scope:
            raise ServiceError("回访类型超出患者同意范围")
        if self.store.get_followup(followup_id):
            raise ServiceError("回访编号已存在")
        if self.quota_balance(agreement_id)["available"] < 1:
            raise ServiceError("服务额度不足，无法预占")
        current = self.store.get_current_list(agreement_id)
        version = current.version if current else 0
        now = self._now()
        followup = FollowUp(
            followup_id=followup_id,
            agreement_id=agreement_id,
            store_id=agreement.store_id,
            kind=kind,
            scheduled_at=scheduled_at,
            status=FOLLOWUP_SCHEDULED,
            based_on_version=version,
            signed_by=None,
            signed_at=None,
        )
        with self.store.transaction():
            self.store.add_followup(followup)
            self.store.add_ledger(
                agreement_id, agreement.store_id, LEDGER_HOLD, 1, followup_id, now,
            )
            self.store.add_audit(
                now, "system", "schedule_followup", agreement_id,
                {"followup_id": followup_id, "kind": kind, "based_on_version": version},
            )
        return self._followup_view(self.store.get_followup(followup_id))

    def sign_followup(self, followup_id: str, staff_id: str, request_key: str | None = None) -> dict:
        payload = {"action": "sign_followup", "followup_id": followup_id, "staff_id": staff_id}
        return self._idempotent(
            request_key, payload, lambda: self._sign_followup(followup_id, staff_id)
        )

    def _sign_followup(self, followup_id, staff_id) -> dict:
        followup = self._must_followup(followup_id)
        agreement = self._must_agreement(followup.agreement_id)
        self._require_active(agreement)
        if followup.status not in _PENDING:
            raise ServiceError("回访当前状态不可签署")
        staff = self._must_staff(staff_id)
        now = self._now()
        # 一次回访只能由签署时点具备资格、且获得当前门店授权的药师签署。
        self._require_pharmacist(staff, now)
        if agreement.store_id not in staff.store_ids:
            raise ServiceError("药师未获得当前门店授权")
        with self.store.transaction():
            self.store.update_followup(
                followup_id, status=FOLLOWUP_DONE, signed_by=staff_id, signed_at=now,
            )
            hold = self.store.find_open_hold(agreement.agreement_id, followup_id)
            if hold:
                self.store.add_ledger(
                    agreement.agreement_id, agreement.store_id, LEDGER_COMPLETE,
                    hold.amount, followup_id, now,
                )
            self.store.add_audit(
                now, staff_id, "sign_followup", agreement.agreement_id,
                {"followup_id": followup_id},
            )
        return self._followup_view(self.store.get_followup(followup_id))

    def cancel_followup(self, followup_id: str, reason: str = "") -> dict:
        followup = self._must_followup(followup_id)
        if followup.status not in _PENDING:
            raise ServiceError("回访当前状态不可取消")
        now = self._now()
        with self.store.transaction():
            self.store.update_followup(followup_id, status=FOLLOWUP_CANCELLED)
            self._refund_open_hold(followup.agreement_id, followup, now)
            self.store.add_audit(
                now, "system", "cancel_followup", followup.agreement_id,
                {"followup_id": followup_id, "reason": reason},
            )
        return self._followup_view(self.store.get_followup(followup_id))

    def replan_followup(self, followup_id: str, scheduled_at: str) -> dict:
        """把被冻结的回访重新挂到当前清单版本上，并重新预占额度。"""
        followup = self._must_followup(followup_id)
        agreement = self._must_agreement(followup.agreement_id)
        self._require_active(agreement)
        if followup.status != FOLLOWUP_FROZEN:
            raise ServiceError("仅被冻结的回访可以重排")
        if self.quota_balance(agreement.agreement_id)["available"] < 1:
            raise ServiceError("服务额度不足，无法预占")
        current = self.store.get_current_list(agreement.agreement_id)
        version = current.version if current else 0
        now = self._now()
        with self.store.transaction():
            self.store.update_followup(
                followup_id, status=FOLLOWUP_SCHEDULED,
                scheduled_at=scheduled_at, based_on_version=version,
            )
            self.store.add_ledger(
                agreement.agreement_id, agreement.store_id, LEDGER_HOLD, 1, followup_id, now,
            )
            self.store.add_audit(
                now, "system", "replan_followup", agreement.agreement_id,
                {"followup_id": followup_id, "based_on_version": version},
            )
        return self._followup_view(self.store.get_followup(followup_id))

    # ---- 异常升级 ----
    def open_escalation(
        self,
        agreement_id: str,
        escalation_id: str,
        kind: str,
        opened_by: str,
        detail: str = "",
    ) -> dict:
        self._must_agreement(agreement_id)
        self._must_staff(opened_by)
        if self.store.get_escalation(escalation_id):
            raise ServiceError("异常编号已存在")
        now = self._now()
        escalation = Escalation(
            escalation_id=escalation_id,
            agreement_id=agreement_id,
            kind=kind,
            status=ESCALATION_OPEN,
            detail=detail,
            opened_by=opened_by,
            created_at=now,
            closed_by=None,
            closed_at=None,
        )
        with self.store.transaction():
            self.store.add_escalation(escalation)
            self.store.add_audit(
                now, opened_by, "open_escalation", agreement_id,
                {"escalation_id": escalation_id, "kind": kind},
            )
        return {"escalation_id": escalation_id, "kind": kind, "status": ESCALATION_OPEN}

    def close_escalation(self, escalation_id: str, staff_id: str) -> dict:
        escalation = self.store.get_escalation(escalation_id)
        if not escalation:
            raise ServiceError("异常不存在")
        if escalation.status != ESCALATION_OPEN:
            raise ServiceError("异常已关闭")
        staff = self._must_staff(staff_id)
        now = self._now()
        if escalation.kind == ESCALATION_SAFETY:
            # 安全异常只能由当时具备资格的药师关闭，销售人员无权关闭。
            self._require_pharmacist(staff, now)
        elif staff.role not in (ROLE_PHARMACIST, ROLE_COMPLIANCE):
            raise ServiceError("销售人员不能关闭异常")
        with self.store.transaction():
            self.store.close_escalation(escalation_id, staff_id, now)
            self.store.add_audit(
                now, staff_id, "close_escalation", escalation.agreement_id,
                {"escalation_id": escalation_id},
            )
        return {"escalation_id": escalation_id, "status": "closed", "closed_by": staff_id}

    # ---- 跨店交接 ----
    def transfer_agreement(self, agreement_id: str, to_store_id: str) -> dict:
        agreement = self._must_agreement(agreement_id)
        self._require_active(agreement)
        from_store = agreement.store_id
        if to_store_id == from_store:
            raise ServiceError("目标门店与当前门店相同")
        now = self._now()
        with self.store.transaction():
            # 原门店的待执行回访取消并退回预占额度，转店后原门店不能继续消费额度。
            cancelled = []
            for followup in self.store.list_followups(agreement_id, status=_PENDING):
                self.store.update_followup(followup.followup_id, status=FOLLOWUP_CANCELLED)
                self._refund_open_hold(agreement_id, followup, now)
                cancelled.append(followup.followup_id)
            self.store.update_agreement(
                agreement_id, store_id=to_store_id, revision=agreement.revision + 1,
            )
            self.store.add_audit(
                now, "system", "transfer_agreement", agreement_id,
                {"from_store": from_store, "to_store": to_store_id,
                 "cancelled_followups": cancelled},
            )
        return {
            "agreement_id": agreement_id,
            "from_store": from_store,
            "to_store": to_store_id,
            "cancelled_followups": cancelled,
        }

    # ---- 支付回调（按业务键去重，内容冲突进入复核） ----
    def payment_callback(self, agreement_id: str, amount: int, request_key: str | None = None) -> dict:
        payload = {
            "action": "payment_callback",
            "agreement_id": agreement_id,
            "amount": amount,
        }
        return self._idempotent(
            request_key, payload,
            lambda: self._payment_callback(agreement_id, amount, request_key),
        )

    def _payment_callback(self, agreement_id, amount, request_key) -> dict:
        self._must_agreement(agreement_id)
        if not isinstance(amount, int) or amount <= 0:
            raise ServiceError("支付额度必须为正整数")
        agreement = self.store.get_agreement(agreement_id)
        now = self._now()
        with self.store.transaction():
            entry_id = self.store.add_ledger(
                agreement_id, agreement.store_id, LEDGER_PURCHASE, amount,
                request_key or "manual", now,
            )
            self.store.add_audit(
                now, "payment", "payment_callback", agreement_id,
                {"amount": amount, "request_key": request_key},
            )
        return {"agreement_id": agreement_id, "entry_id": entry_id, "purchased": amount}

    # ---- 复核 ----
    def resolve_review(self, review_id: str, staff_id: str, resolution: str) -> dict:
        review = self.store.get_review(review_id)
        if not review:
            raise ServiceError("复核单不存在")
        if review.status != REVIEW_OPEN:
            raise ServiceError("复核单已处理")
        staff = self._must_staff(staff_id)
        if staff.role != ROLE_COMPLIANCE:
            raise ServiceError("仅合规人员可以处理复核")
        with self.store.transaction():
            self.store.resolve_review(review_id, staff_id, resolution)
            self.store.add_audit(
                self._now(), staff_id, "resolve_review", None,
                {"review_id": review_id, "resolution": resolution},
            )
        return {"review_id": review_id, "status": "resolved"}

    # ---- 角色视图 ----
    def view(self, agreement_id: str, role: str) -> dict:
        agreement = self._must_agreement(agreement_id)
        if role == ROLE_PATIENT:
            # 患者只看得到计划与同意状态，看不到费用与内部异常。
            plan = [
                {
                    "followup_id": f.followup_id,
                    "kind": f.kind,
                    "scheduled_at": f.scheduled_at,
                    "status": f.status,
                    "store_id": f.store_id,
                }
                for f in self.store.list_followups(agreement_id, status=_PENDING)
            ]
            return {
                "role": role,
                "agreement_id": agreement_id,
                "state": agreement.state,
                "consent": {
                    "status": agreement.consent_status,
                    "scope": list(agreement.consent_scope),
                },
                "goals": [g.text for g in self.store.list_goals(agreement_id)],
                "plan": plan,
            }
        if role == ROLE_PHARMACIST:
            # 药师看到执行依据：当前清单版本、目标、回访及其依据版本、未关闭异常。
            current = self.store.get_current_list(agreement_id)
            return {
                "role": role,
                "agreement_id": agreement_id,
                "state": agreement.state,
                "store_id": agreement.store_id,
                "medication_list": (
                    {
                        "version": current.version,
                        "items": list(current.items),
                        "source": current.source,
                    }
                    if current else None
                ),
                "goals": [g.text for g in self.store.list_goals(agreement_id)],
                "followups": [
                    self._followup_view(f) for f in self.store.list_followups(agreement_id)
                ],
                "open_escalations": [
                    self._escalation_view(e)
                    for e in self.store.list_escalations(agreement_id, status=ESCALATION_OPEN)
                ],
            }
        if role == ROLE_COMPLIANCE:
            # 合规人员看到未解决风险与费用去向，以及依法保留的审计历史。
            return {
                "role": role,
                "agreement_id": agreement_id,
                "state": agreement.state,
                "open_escalations": [
                    self._escalation_view(e)
                    for e in self.store.list_escalations(agreement_id, status=ESCALATION_OPEN)
                ],
                "open_reviews": [
                    {"review_id": r.review_id, "request_key": r.request_key}
                    for r in self.store.list_reviews(status=REVIEW_OPEN)
                ],
                "overdue_followups": [
                    f.followup_id
                    for f in self.store.list_followups(agreement_id, status=FOLLOWUP_OVERDUE)
                ],
                "ledger": [
                    {
                        "entry_id": e.entry_id,
                        "store_id": e.store_id,
                        "kind": e.kind,
                        "amount": e.amount,
                        "ref_id": e.ref_id,
                        "created_at": e.created_at,
                    }
                    for e in self.store.list_ledger(agreement_id)
                ],
                "quota": self.quota_balance(agreement_id),
                "audit_trail": self.store.list_audit(agreement_id),
            }
        raise ServiceError(f"未知的查看角色: {role}")

    # ---- 额度 ----
    def quota_balance(self, agreement_id: str) -> dict:
        entries = self.store.list_ledger(agreement_id)
        purchased = sum(e.amount for e in entries if e.kind == LEDGER_PURCHASE)
        held = sum(e.amount for e in entries if e.kind == LEDGER_HOLD)
        completed = sum(e.amount for e in entries if e.kind == LEDGER_COMPLETE)
        refunded = sum(e.amount for e in entries if e.kind == LEDGER_REFUND)
        return {
            "purchased": purchased,
            "held_open": held - completed - refunded,
            "completed": completed,
            "refunded": refunded,
            "available": purchased - held + refunded,
        }

    # ---- 重启恢复 ----
    def recover(self) -> dict:
        """把已过期的待办回访标记为逾期，并返回升级与复核队列。"""
        now = _parse(self._now())
        overdue = []
        with self.store.transaction():
            for followup in self.store.list_followups(status=FOLLOWUP_SCHEDULED):
                if _parse(followup.scheduled_at) < now:
                    self.store.update_followup(followup.followup_id, status=FOLLOWUP_OVERDUE)
                    overdue.append(followup.followup_id)
        return {
            "overdue_followups": overdue,
            "open_escalations": [
                e.escalation_id for e in self.store.list_escalations(status=ESCALATION_OPEN)
            ],
            "open_reviews": [r.review_id for r in self.store.list_reviews(status=REVIEW_OPEN)],
        }

    # ---- 内部：幂等 ----
    def _idempotent(self, request_key, payload, fn):
        if not request_key:
            return fn()
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        receipt = self.store.get_receipt(request_key)
        if receipt:
            if receipt["payload_hash"] == digest:
                result = json.loads(receipt["response_json"])
                result["duplicate"] = True
                return result
            review_id = f"review-{request_key}"
            with self.store.transaction():
                self.store.put_review(
                    review_id, request_key, receipt["response_json"], canonical, self._now(),
                )
                self.store.add_audit(
                    self._now(), "system", "conflict_review", None,
                    {"review_id": review_id, "request_key": request_key},
                )
            return {"status": "conflict", "review_id": review_id, "request_key": request_key}
        result = fn()
        self.store.put_receipt(
            request_key, digest, json.dumps(result, ensure_ascii=False, sort_keys=True)
        )
        return result

    # ---- 内部：规则与工具 ----
    def _freeze_pending_followups(self, agreement, now, reason, only_below_version=None) -> list:
        frozen = []
        for followup in self.store.list_followups(agreement.agreement_id, status=_PENDING):
            if only_below_version is not None and followup.based_on_version >= only_below_version:
                continue
            self.store.update_followup(followup.followup_id, status=FOLLOWUP_FROZEN)
            self._refund_open_hold(agreement.agreement_id, followup, now)
            frozen.append(followup.followup_id)
        return frozen

    def _refund_open_hold(self, agreement_id, followup, now) -> None:
        hold = self.store.find_open_hold(agreement_id, followup.followup_id)
        if hold:
            self.store.add_ledger(
                agreement_id, followup.store_id, LEDGER_REFUND, hold.amount,
                followup.followup_id, now,
            )

    def _require_pharmacist(self, staff, moment: str) -> None:
        if staff.role != ROLE_PHARMACIST:
            raise ServiceError("需要具备药师角色")
        if PHARMACIST_QUALIFICATION not in staff.qualifications:
            raise ServiceError("缺少执业药师资格")
        if not (_parse(staff.valid_from) <= _parse(moment) <= _parse(staff.valid_until)):
            raise ServiceError("药师资格在该时点无效")

    @staticmethod
    def _require_active(agreement) -> None:
        if agreement.state != AGREEMENT_ACTIVE:
            raise ServiceError("协议已冻结，未来动作已停止")

    def _must_agreement(self, agreement_id) -> Agreement:
        agreement = self.store.get_agreement(agreement_id)
        if not agreement:
            raise ServiceError("协议不存在")
        return agreement

    def _must_staff(self, staff_id) -> Staff:
        staff = self.store.get_staff(staff_id)
        if not staff:
            raise ServiceError("人员不存在")
        return staff

    def _must_followup(self, followup_id) -> FollowUp:
        followup = self.store.get_followup(followup_id)
        if not followup:
            raise ServiceError("回访不存在")
        return followup

    @staticmethod
    def _agreement_view(agreement) -> dict:
        return {
            "agreement_id": agreement.agreement_id,
            "patient_id": agreement.patient_id,
            "store_id": agreement.store_id,
            "state": agreement.state,
            "consent_status": agreement.consent_status,
            "consent_scope": list(agreement.consent_scope),
            "revision": agreement.revision,
        }

    @staticmethod
    def _followup_view(followup) -> dict:
        return {
            "followup_id": followup.followup_id,
            "agreement_id": followup.agreement_id,
            "store_id": followup.store_id,
            "kind": followup.kind,
            "scheduled_at": followup.scheduled_at,
            "status": followup.status,
            "based_on_version": followup.based_on_version,
            "signed_by": followup.signed_by,
            "signed_at": followup.signed_at,
        }

    @staticmethod
    def _escalation_view(escalation) -> dict:
        return {
            "escalation_id": escalation.escalation_id,
            "kind": escalation.kind,
            "status": escalation.status,
            "detail": escalation.detail,
            "opened_by": escalation.opened_by,
            "created_at": escalation.created_at,
        }
