"""Shared AuditLog writer. Used by the injection guard (chunk quarantine
events, logged during a graph run before any Briefing row exists yet --
hence AuditLog.briefingId is nullable) and by compliance_critic_node
(final verdict events). Kept outside backend/compliance/ because it's a
cross-cutting concern other phases will also want (Phase 7's approval
workflow is explicitly supposed to audit PATCH /approve|reject actions),
not compliance-specific logic.

Never raises: an audit-log write failure should never take down the graph
run it's trying to record. Logged and swallowed instead."""

import logging

from prisma import Json

logger = logging.getLogger("audit")


def log_event(
    db, action: str, user_id: str = None, client_id: str = None, briefing_id: str = None, metadata: dict = None
) -> None:
    try:
        db.auditlog.create(
            data={
                "userId": user_id,
                "clientId": client_id,
                "briefingId": briefing_id,
                "action": action,
                "metadata": Json(metadata or {}),
            }
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to write AuditLog event action={action}: {e}")
