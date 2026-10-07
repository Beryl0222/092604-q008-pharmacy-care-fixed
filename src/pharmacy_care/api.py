"""处理进程内 JSON 请求。"""
import json

from .service import Service, ServiceError


def _dispatch(current: Service, action, body: dict):
    if action == "health":
        return current.health()
    if action == "register":
        return current.register(str(body["record_id"]), str(body["owner_id"]))
    if action == "find":
        return current.find(str(body["record_id"]))
    if action == "register_staff":
        return current.register_staff(
            str(body["staff_id"]),
            str(body["role"]),
            qualifications=body.get("qualifications", ()),
            store_ids=body.get("store_ids", ()),
            valid_from=body.get("valid_from"),
            valid_until=body.get("valid_until"),
        )
    if action == "open_agreement":
        return current.open_agreement(
            str(body["agreement_id"]),
            str(body["patient_id"]),
            str(body["store_id"]),
            body.get("consent_scope", ()),
            request_key=body.get("request_key"),
        )
    if action == "withdraw_consent":
        return current.withdraw_consent(
            str(body["agreement_id"]), request_key=body.get("request_key")
        )
    if action == "record_medication_list":
        return current.record_medication_list(
            str(body["agreement_id"]),
            body.get("items", ()),
            source=str(body.get("source", "prescription")),
            request_key=body.get("request_key"),
        )
    if action == "add_goal":
        return current.add_goal(
            str(body["agreement_id"]), str(body["text"]), goal_id=body.get("goal_id")
        )
    if action == "schedule_followup":
        return current.schedule_followup(
            str(body["agreement_id"]),
            str(body["followup_id"]),
            str(body["kind"]),
            str(body["scheduled_at"]),
            request_key=body.get("request_key"),
        )
    if action == "sign_followup":
        return current.sign_followup(
            str(body["followup_id"]), str(body["staff_id"]),
            request_key=body.get("request_key"),
        )
    if action == "cancel_followup":
        return current.cancel_followup(
            str(body["followup_id"]), reason=str(body.get("reason", ""))
        )
    if action == "replan_followup":
        return current.replan_followup(
            str(body["followup_id"]), str(body["scheduled_at"])
        )
    if action == "open_escalation":
        return current.open_escalation(
            str(body["agreement_id"]),
            str(body["escalation_id"]),
            str(body["kind"]),
            str(body["opened_by"]),
            detail=str(body.get("detail", "")),
        )
    if action == "close_escalation":
        return current.close_escalation(str(body["escalation_id"]), str(body["staff_id"]))
    if action == "transfer_agreement":
        return current.transfer_agreement(
            str(body["agreement_id"]), str(body["to_store_id"])
        )
    if action == "payment_callback":
        return current.payment_callback(
            str(body["agreement_id"]), int(body["amount"]),
            request_key=body.get("request_key"),
        )
    if action == "resolve_review":
        return current.resolve_review(
            str(body["review_id"]), str(body["staff_id"]), str(body.get("resolution", ""))
        )
    if action == "view":
        return current.view(str(body["agreement_id"]), str(body["role"]))
    if action == "quota_balance":
        return current.quota_balance(str(body["agreement_id"]))
    if action == "recover":
        return current.recover()
    raise ValueError("不支持的请求动作")


def handle(raw: str, service: Service | None = None) -> str:
    current = service or Service()
    body = json.loads(raw)
    action = body.get("action")
    try:
        result = _dispatch(current, action, body)
    except ServiceError as exc:
        result = {"error": str(exc)}
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
