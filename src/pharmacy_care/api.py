"""处理进程内 JSON 请求。"""
import json

from .domain import DomainError
from .service import Service

_ACTIONS = {
    "health",
    "register",
    "find",
    "create_protocol",
    "get_protocol",
    "grant_consent",
    "withdraw_consent",
    "register_practitioner",
    "add_medication_list",
    "add_goal",
    "schedule_followup",
    "complete_followup",
    "backfill_followup",
    "open_escalation",
    "close_escalation",
    "hold_quota",
    "release_quota",
    "transfer_store",
    "record_payment",
    "patient_view",
    "pharmacist_view",
    "compliance_view",
    "recover",
}


def handle(raw: str, service: Service | None = None) -> str:
    current = service or Service()
    body = json.loads(raw)
    action = body.get("action")
    if action not in _ACTIONS:
        raise ValueError("不支持的请求动作")
    method = getattr(current, action)
    kwargs = {key: value for key, value in body.items() if key != "action"}
    try:
        result = method(**kwargs)
    except DomainError as exc:
        result = {"error": str(exc), "error_type": type(exc).__name__}
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
