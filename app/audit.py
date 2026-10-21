"""Append-only audit trail. Written in its own short transaction (a separate session) so the
record survives even when the operation around it rolls back."""
import logging

from sqlalchemy.orm import Session

from .db import engine
from .models import AuditEntry

logger = logging.getLogger(__name__)


def record(actor: str, action: str, *, target: str = "", status: str = "ok",
           detail: str = "") -> None:
    try:
        with Session(engine) as db:
            db.add(AuditEntry(actor=actor or "", action=action, target=(target or "")[:254],
                              status=status, detail=(detail or "")[:4000]))
            db.commit()
    except Exception:  # noqa: BLE001 — auditing must never break the operation
        logger.exception("Failed to write audit entry for action=%s", action)
