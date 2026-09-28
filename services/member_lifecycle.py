"""将新鲜群成员快照同步到当期关系和补抽资格。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Set

from .storage import SQLiteStorage


def reconcile_member_eligibility(
    storage: SQLiteStorage,
    *,
    scope_id: int,
    period_id: str,
    eligible_user_ids: Set[str],
    now: int,
) -> int:
    """解除失去参与资格者相关的关系，并为仍合资格的失配偶持有人补偿一次。"""

    with storage.transaction() as db:
        rows = db.execute(
            """SELECT r.*,s.name FROM relationships r JOIN subject_snapshots s ON s.id=r.snapshot_id
               WHERE r.scope_id=? AND r.period_id=? AND r.state='active'""",
            (scope_id, period_id),
        ).fetchall()
        affected: list[str] = []
        removed = 0
        for row in rows:
            owner_id = str(row["owner_id"])
            subject_id = str(row["subject_id"])
            owner_eligible = owner_id in eligible_user_ids
            subject_lost = row["subject_kind"] == "member" and subject_id not in eligible_user_ids
            if owner_eligible and not subject_lost:
                continue
            reason = "member_removed" if subject_lost and owner_eligible else "owner_ineligible"
            updated = db.execute(
                "UPDATE relationships SET state='ended',ended_at=?,end_reason=?,version=version+1 WHERE id=? AND state='active'",
                (now, reason, row["id"]),
            )
            if updated.rowcount != 1:
                continue
            removed += 1
            db.execute(
                "UPDATE gift_invites SET state='invalidated',finalized_at=?,reason=? WHERE relationship_id=? AND state='pending'",
                (now, reason, row["id"]),
            )
            if reason == "member_removed":
                operation_id = f"member-removed:{row['id']}"
                db.execute(
                    """INSERT OR IGNORE INTO redraw_credits(
                           id,scope_id,period_id,mode,user_id,source_operation_id,reason,state,issued_at
                       ) VALUES(?,?,?,?,?,?,'member_removed','available',?)""",
                    (str(uuid.uuid4()), scope_id, period_id, row["mode"], owner_id, operation_id, now),
                )
                db.execute(
                    """INSERT OR IGNORE INTO operation_log(
                           id,scope_id,period_id,mode,kind,actor_id,actor_type,result_code,config_revision,data_json,created_at
                       ) VALUES(?,?,?,?,?,'system','system','MEMBER_REMOVED',?, ?,?)""",
                    (operation_id, scope_id, period_id, row["mode"], "member_reconcile", "", json.dumps({"owner_id": owner_id, "subject_id": subject_id, "name": row["name"]}, ensure_ascii=False), now),
                )
                affected.append(f"{row['name']}（持有人 {owner_id} 获得补抽资格）")
            else:
                affected.append(f"{row['name']}（持有人 {owner_id} 已失去参与资格）")
        invites = db.execute(
            """SELECT id,sender_id,recipient_id FROM gift_invites
               WHERE scope_id=? AND period_id=? AND state='pending'""",
            (scope_id, period_id),
        ).fetchall()
        for invite in invites:
            sender_id = str(invite["sender_id"])
            recipient_id = str(invite["recipient_id"])
            if sender_id in eligible_user_ids and recipient_id in eligible_user_ids:
                continue
            db.execute(
                "UPDATE gift_invites SET state='invalidated',finalized_at=?,reason='participant_ineligible' WHERE id=? AND state='pending'",
                (now, invite["id"]),
            )
            affected.append(f"赠送邀请 {invite['id']}（参与者已失去资格）")
        if affected:
            scope = db.execute("SELECT umo FROM scopes WHERE id=?", (scope_id,)).fetchone()
            if scope:
                db.execute(
                    """INSERT OR IGNORE INTO notification_outbox(id,scope_id,dedupe_key,umo,payload_json,state,created_at)
                       VALUES(?,?,?,?,?,'pending',?)""",
                    (
                        str(uuid.uuid4()),
                        scope_id,
                        f"member-reconcile:{period_id}:{uuid.uuid4().hex}",
                        str(scope["umo"]),
                        json.dumps({"text": "成员资格变化，以下群友关系已解除：" + "；".join(affected)}, ensure_ascii=False),
                        now,
                    ),
                )
        return removed
