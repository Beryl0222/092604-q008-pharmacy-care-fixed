"""药师连续照护协议的领域常量、基础对象与业务错误。"""
from dataclasses import dataclass


class DomainError(Exception):
    """业务规则被拒绝。"""


class NotFoundError(DomainError):
    """目标对象不存在。"""


class ConflictError(DomainError):
    """业务键内容冲突，已转入复核。"""


@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str
    revision: int
    created_at: str


# 协议状态
PROTOCOL_ACTIVE = "active"
PROTOCOL_FROZEN = "frozen"

# 同意状态
CONSENT_GRANTED = "granted"
CONSENT_WITHDRAWN = "withdrawn"

# 回访状态
FOLLOWUP_SCHEDULED = "scheduled"
FOLLOWUP_DONE = "done"
FOLLOWUP_FROZEN = "frozen"

# 异常状态
ESCALATION_OPEN = "open"
ESCALATION_CLOSED = "closed"

# 复核状态
REVIEW_OPEN = "open"

# 人员角色
ROLE_PHARMACIST = "pharmacist"
ROLE_SALES = "sales"
ROLE_COMPLIANCE = "compliance"

# 额度流水类型
QUOTA_HOLD = "hold"
QUOTA_CONSUME = "consume"
QUOTA_RELEASE = "release"
