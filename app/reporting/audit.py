"""Audit event sinks with no secret-bearing free-form payload."""

import logging
from threading import RLock
from typing import Protocol

from app.domain.audit import AuditEvent


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class NullAuditSink:
    def record(self, event: AuditEvent) -> None:
        del event


class InMemoryAuditSink:
    def __init__(self) -> None:
        self._lock = RLock()
        self._events: list[AuditEvent] = []

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)


class JsonLoggingAuditSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("aegis_invest.audit")

    def record(self, event: AuditEvent) -> None:
        self._logger.info(
            "audit_event",
            extra={"structured_event": event.model_dump(mode="json")},
        )
