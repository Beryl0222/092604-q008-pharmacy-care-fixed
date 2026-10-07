"""使用 SQLite 保存连续照护协议数据、额度流水、幂等回执与审计记录。"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .domain import (
    Agreement,
    Escalation,
    FollowUp,
    Goal,
    LedgerEntry,
    MedicationList,
    Record,
    Review,
    Staff,
)


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS request_receipts (
                request_key TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                response_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS staff (
                staff_id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                qualifications_json TEXT NOT NULL,
                store_ids_json TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_until TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agreements (
                agreement_id TEXT PRIMARY KEY,
                patient_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                state TEXT NOT NULL,
                consent_scope_json TEXT NOT NULL,
                consent_status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS medication_lists (
                list_id TEXT PRIMARY KEY,
                agreement_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                items_json TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(agreement_id, version)
            );
            CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY,
                agreement_id TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS followups (
                followup_id TEXT PRIMARY KEY,
                agreement_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL,
                based_on_version INTEGER NOT NULL,
                signed_by TEXT,
                signed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS escalations (
                escalation_id TEXT PRIMARY KEY,
                agreement_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL,
                opened_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                closed_by TEXT,
                closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ledger (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agreement_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                amount INTEGER NOT NULL,
                ref_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY,
                request_key TEXT NOT NULL,
                stored_payload TEXT NOT NULL,
                incoming_payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_by TEXT,
                resolution TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                agreement_id TEXT,
                detail_json TEXT NOT NULL
            );
        """)
        self.connection.commit()

    @contextmanager
    def transaction(self):
        """把多次写入放进同一事务，任一失败整体回滚。"""
        with self.connection:
            yield

    # ---- 基础登记（保留） ----
    def add(self, record: Record) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO records(record_id,owner_id,state,revision,created_at) VALUES(?,?,?,?,?)",
                (record.record_id, record.owner_id, record.state, record.revision, record.created_at),
            )

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id,owner_id,state,revision,created_at FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    # ---- 人员与授权 ----
    def add_staff(self, staff: Staff) -> None:
        self.connection.execute(
            "INSERT INTO staff(staff_id,role,qualifications_json,store_ids_json,valid_from,valid_until)"
            " VALUES(?,?,?,?,?,?)",
            (
                staff.staff_id,
                staff.role,
                json.dumps(list(staff.qualifications), ensure_ascii=False),
                json.dumps(list(staff.store_ids), ensure_ascii=False),
                staff.valid_from,
                staff.valid_until,
            ),
        )

    def get_staff(self, staff_id: str) -> Staff | None:
        row = self.connection.execute(
            "SELECT * FROM staff WHERE staff_id=?", (staff_id,)
        ).fetchone()
        if not row:
            return None
        return Staff(
            staff_id=row["staff_id"],
            role=row["role"],
            qualifications=tuple(json.loads(row["qualifications_json"])),
            store_ids=tuple(json.loads(row["store_ids_json"])),
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
        )

    # ---- 协议 ----
    def add_agreement(self, agreement: Agreement) -> None:
        self.connection.execute(
            "INSERT INTO agreements(agreement_id,patient_id,store_id,state,consent_scope_json,"
            "consent_status,revision,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                agreement.agreement_id,
                agreement.patient_id,
                agreement.store_id,
                agreement.state,
                json.dumps(list(agreement.consent_scope), ensure_ascii=False),
                agreement.consent_status,
                agreement.revision,
                agreement.created_at,
            ),
        )

    def get_agreement(self, agreement_id: str) -> Agreement | None:
        row = self.connection.execute(
            "SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if not row:
            return None
        return Agreement(
            agreement_id=row["agreement_id"],
            patient_id=row["patient_id"],
            store_id=row["store_id"],
            state=row["state"],
            consent_scope=tuple(json.loads(row["consent_scope_json"])),
            consent_status=row["consent_status"],
            revision=row["revision"],
            created_at=row["created_at"],
        )

    def update_agreement(self, agreement_id: str, **fields) -> None:
        columns = {
            "state": fields.get("state"),
            "store_id": fields.get("store_id"),
            "consent_status": fields.get("consent_status"),
            "revision": fields.get("revision"),
        }
        sets = [f"{name}=?" for name, value in columns.items() if value is not None]
        values = [value for value in columns.values() if value is not None]
        if not sets:
            return
        self.connection.execute(
            f"UPDATE agreements SET {', '.join(sets)} WHERE agreement_id=?",
            (*values, agreement_id),
        )

    # ---- 药物清单版本 ----
    def add_medication_list(self, med_list: MedicationList) -> None:
        self.connection.execute(
            "INSERT INTO medication_lists(list_id,agreement_id,version,items_json,source,status,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                med_list.list_id,
                med_list.agreement_id,
                med_list.version,
                json.dumps(list(med_list.items), ensure_ascii=False),
                med_list.source,
                med_list.status,
                med_list.created_at,
            ),
        )

    def get_current_list(self, agreement_id: str) -> MedicationList | None:
        row = self.connection.execute(
            "SELECT * FROM medication_lists WHERE agreement_id=? AND status='current'"
            " ORDER BY version DESC LIMIT 1",
            (agreement_id,),
        ).fetchone()
        return self._to_list(row) if row else None

    def list_medication_lists(self, agreement_id: str) -> list[MedicationList]:
        rows = self.connection.execute(
            "SELECT * FROM medication_lists WHERE agreement_id=? ORDER BY version",
            (agreement_id,),
        ).fetchall()
        return [self._to_list(row) for row in rows]

    def supersede_current_list(self, agreement_id: str) -> None:
        self.connection.execute(
            "UPDATE medication_lists SET status='superseded' WHERE agreement_id=? AND status='current'",
            (agreement_id,),
        )

    @staticmethod
    def _to_list(row: sqlite3.Row) -> MedicationList:
        return MedicationList(
            list_id=row["list_id"],
            agreement_id=row["agreement_id"],
            version=row["version"],
            items=tuple(json.loads(row["items_json"])),
            source=row["source"],
            status=row["status"],
            created_at=row["created_at"],
        )

    # ---- 服务目标 ----
    def add_goal(self, goal: Goal) -> None:
        self.connection.execute(
            "INSERT INTO goals(goal_id,agreement_id,text,created_at) VALUES(?,?,?,?)",
            (goal.goal_id, goal.agreement_id, goal.text, goal.created_at),
        )

    def list_goals(self, agreement_id: str) -> list[Goal]:
        rows = self.connection.execute(
            "SELECT * FROM goals WHERE agreement_id=? ORDER BY created_at, goal_id",
            (agreement_id,),
        ).fetchall()
        return [Goal(**dict(row)) for row in rows]

    # ---- 回访事件 ----
    def add_followup(self, followup: FollowUp) -> None:
        self.connection.execute(
            "INSERT INTO followups(followup_id,agreement_id,store_id,kind,scheduled_at,status,"
            "based_on_version,signed_by,signed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                followup.followup_id,
                followup.agreement_id,
                followup.store_id,
                followup.kind,
                followup.scheduled_at,
                followup.status,
                followup.based_on_version,
                followup.signed_by,
                followup.signed_at,
            ),
        )

    def get_followup(self, followup_id: str) -> FollowUp | None:
        row = self.connection.execute(
            "SELECT * FROM followups WHERE followup_id=?", (followup_id,)
        ).fetchone()
        return FollowUp(**dict(row)) if row else None

    def update_followup(self, followup_id: str, **fields) -> None:
        allowed = ("status", "scheduled_at", "based_on_version", "signed_by", "signed_at")
        sets = [f"{name}=?" for name in allowed if name in fields]
        values = [fields[name] for name in allowed if name in fields]
        if not sets:
            return
        self.connection.execute(
            f"UPDATE followups SET {', '.join(sets)} WHERE followup_id=?",
            (*values, followup_id),
        )

    def list_followups(
        self,
        agreement_id: str | None = None,
        status: str | tuple | None = None,
    ) -> list[FollowUp]:
        sql = "SELECT * FROM followups"
        clauses, params = [], []
        if agreement_id is not None:
            clauses.append("agreement_id=?")
            params.append(agreement_id)
        if status is not None:
            statuses = (status,) if isinstance(status, str) else tuple(status)
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY scheduled_at, followup_id"
        rows = self.connection.execute(sql, params).fetchall()
        return [FollowUp(**dict(row)) for row in rows]

    # ---- 异常升级 ----
    def add_escalation(self, escalation: Escalation) -> None:
        self.connection.execute(
            "INSERT INTO escalations(escalation_id,agreement_id,kind,status,detail,opened_by,"
            "created_at,closed_by,closed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                escalation.escalation_id,
                escalation.agreement_id,
                escalation.kind,
                escalation.status,
                escalation.detail,
                escalation.opened_by,
                escalation.created_at,
                escalation.closed_by,
                escalation.closed_at,
            ),
        )

    def get_escalation(self, escalation_id: str) -> Escalation | None:
        row = self.connection.execute(
            "SELECT * FROM escalations WHERE escalation_id=?", (escalation_id,)
        ).fetchone()
        return Escalation(**dict(row)) if row else None

    def close_escalation(self, escalation_id: str, closed_by: str, closed_at: str) -> None:
        self.connection.execute(
            "UPDATE escalations SET status='closed', closed_by=?, closed_at=? WHERE escalation_id=?",
            (closed_by, closed_at, escalation_id),
        )

    def list_escalations(
        self, agreement_id: str | None = None, status: str | None = None
    ) -> list[Escalation]:
        sql = "SELECT * FROM escalations"
        clauses, params = [], []
        if agreement_id is not None:
            clauses.append("agreement_id=?")
            params.append(agreement_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, escalation_id"
        rows = self.connection.execute(sql, params).fetchall()
        return [Escalation(**dict(row)) for row in rows]

    # ---- 服务额度流水 ----
    def add_ledger(
        self,
        agreement_id: str,
        store_id: str,
        kind: str,
        amount: int,
        ref_id: str,
        created_at: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO ledger(agreement_id,store_id,kind,amount,ref_id,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (agreement_id, store_id, kind, amount, ref_id, created_at),
        )
        return cursor.lastrowid

    def list_ledger(self, agreement_id: str) -> list[LedgerEntry]:
        rows = self.connection.execute(
            "SELECT * FROM ledger WHERE agreement_id=? ORDER BY entry_id",
            (agreement_id,),
        ).fetchall()
        return [LedgerEntry(**dict(row)) for row in rows]

    def find_open_hold(self, agreement_id: str, ref_id: str) -> LedgerEntry | None:
        """找到尚未完成也未退回的预占记录。"""
        row = self.connection.execute(
            "SELECT * FROM ledger h WHERE h.agreement_id=? AND h.ref_id=? AND h.kind='hold'"
            " AND NOT EXISTS (SELECT 1 FROM ledger s WHERE s.ref_id=h.ref_id"
            " AND s.kind IN ('complete','refund'))"
            " ORDER BY h.entry_id LIMIT 1",
            (agreement_id, ref_id),
        ).fetchone()
        return LedgerEntry(**dict(row)) if row else None

    # ---- 幂等回执与复核 ----
    def get_receipt(self, request_key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT request_key,payload_hash,response_json FROM request_receipts WHERE request_key=?",
            (request_key,),
        ).fetchone()
        return dict(row) if row else None

    def put_receipt(self, request_key: str, payload_hash: str, response_json: str) -> None:
        self.connection.execute(
            "INSERT INTO request_receipts(request_key,payload_hash,response_json) VALUES(?,?,?)",
            (request_key, payload_hash, response_json),
        )

    def put_review(
        self,
        review_id: str,
        request_key: str,
        stored_payload: str,
        incoming_payload: str,
        created_at: str,
    ) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO reviews(review_id,request_key,stored_payload,incoming_payload,"
            "status,created_at,resolved_by,resolution) VALUES(?,?,?,?, 'open', ?, NULL, NULL)",
            (review_id, request_key, stored_payload, incoming_payload, created_at),
        )

    def get_review(self, review_id: str) -> Review | None:
        row = self.connection.execute(
            "SELECT * FROM reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        return Review(**dict(row)) if row else None

    def list_reviews(self, status: str | None = None) -> list[Review]:
        if status is None:
            rows = self.connection.execute("SELECT * FROM reviews ORDER BY created_at").fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM reviews WHERE status=? ORDER BY created_at", (status,)
            ).fetchall()
        return [Review(**dict(row)) for row in rows]

    def resolve_review(self, review_id: str, resolved_by: str, resolution: str) -> None:
        self.connection.execute(
            "UPDATE reviews SET status='resolved', resolved_by=?, resolution=? WHERE review_id=?",
            (resolved_by, resolution, review_id),
        )

    # ---- 审计（依法保留，不随同意撤回删除） ----
    def add_audit(
        self,
        at: str,
        actor: str,
        action: str,
        agreement_id: str | None,
        detail: dict,
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_log(at,actor,action,agreement_id,detail_json) VALUES(?,?,?,?,?)",
            (at, actor, action, agreement_id, json.dumps(detail, ensure_ascii=False, sort_keys=True)),
        )

    def list_audit(self, agreement_id: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM audit_log WHERE agreement_id=? ORDER BY audit_id",
            (agreement_id,),
        ).fetchall()
        return [
            {
                "audit_id": row["audit_id"],
                "at": row["at"],
                "actor": row["actor"],
                "action": row["action"],
                "agreement_id": row["agreement_id"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]
