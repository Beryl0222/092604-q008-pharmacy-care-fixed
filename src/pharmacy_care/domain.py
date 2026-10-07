"""药师连续照护协议的领域对象与状态常量。"""
from dataclasses import dataclass, field


# ---- 角色 ----
ROLE_PATIENT = "patient"
ROLE_PHARMACIST = "pharmacist"
ROLE_SALES = "sales"
ROLE_COMPLIANCE = "compliance"
STAFF_ROLES = (ROLE_PHARMACIST, ROLE_SALES, ROLE_COMPLIANCE)
#: 签署回访所要求的执业资格
PHARMACIST_QUALIFICATION = "licensed_pharmacist"

# ---- 协议状态 ----
AGREEMENT_ACTIVE = "active"
AGREEMENT_FROZEN = "frozen"  # 患者撤回同意后冻结，历史记录保留

# ---- 同意状态 ----
CONSENT_GRANTED = "granted"
CONSENT_WITHDRAWN = "withdrawn"

# ---- 药物清单状态 ----
LIST_CURRENT = "current"
LIST_SUPERSEDED = "superseded"

# ---- 回访状态 ----
FOLLOWUP_SCHEDULED = "scheduled"
FOLLOWUP_OVERDUE = "overdue"
FOLLOWUP_DONE = "done"
FOLLOWUP_CANCELLED = "cancelled"
FOLLOWUP_FROZEN = "frozen"  # 依赖的信息（处方/同意）已失效，等待重排

# ---- 异常升级 ----
ESCALATION_OPEN = "open"
ESCALATION_CLOSED = "closed"
ESCALATION_SAFETY = "safety"

# ---- 额度流水种类 ----
LEDGER_PURCHASE = "purchase"
LEDGER_HOLD = "hold"
LEDGER_COMPLETE = "complete"
LEDGER_REFUND = "refund"

# ---- 复核 ----
REVIEW_OPEN = "open"
REVIEW_RESOLVED = "resolved"


@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str
    revision: int
    created_at: str


@dataclass(frozen=True)
class Staff:
    staff_id: str
    role: str
    qualifications: tuple
    store_ids: tuple
    valid_from: str
    valid_until: str


@dataclass(frozen=True)
class Agreement:
    agreement_id: str
    patient_id: str
    store_id: str
    state: str
    consent_scope: tuple
    consent_status: str
    revision: int
    created_at: str


@dataclass(frozen=True)
class MedicationList:
    list_id: str
    agreement_id: str
    version: int
    items: tuple
    source: str
    status: str
    created_at: str


@dataclass(frozen=True)
class Goal:
    goal_id: str
    agreement_id: str
    text: str
    created_at: str


@dataclass(frozen=True)
class FollowUp:
    followup_id: str
    agreement_id: str
    store_id: str
    kind: str
    scheduled_at: str
    status: str
    based_on_version: int
    signed_by: str | None
    signed_at: str | None


@dataclass(frozen=True)
class Escalation:
    escalation_id: str
    agreement_id: str
    kind: str
    status: str
    detail: str
    opened_by: str
    created_at: str
    closed_by: str | None
    closed_at: str | None


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: int
    agreement_id: str
    store_id: str
    kind: str
    amount: int
    ref_id: str
    created_at: str


@dataclass(frozen=True)
class Review:
    review_id: str
    request_key: str
    stored_payload: str
    incoming_payload: str
    status: str
    created_at: str
    resolved_by: str | None
    resolution: str | None
