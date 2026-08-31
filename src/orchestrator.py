from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock
from typing import TypedDict

from CTFd.models import db

from .coordination import InstanceCoordinator
from .docker_host_manager import DockerHostManager
from .event_logger import MetadataDict, event_logger
from .models import ContainerChallengeModel, DockerContextModel

logger = logging.getLogger(__name__)


class HostStatus(TypedDict):
    context_name: str
    pub_hostname: str | None
    active_containers: int
    healthy: bool
    weight: int
    score: float


class Orchestrator:
    """DB-derived host health and load reporting."""

    def __init__(self, host_manager: DockerHostManager) -> None:
        self.host_manager = host_manager
        self.container_counts: defaultdict[str, int] = defaultdict(int)
        self.health: dict[str, bool] = {}
        self.weights: dict[str, int] = {}
        self.lock = Lock()

    @staticmethod
    def _challenge_image() -> str | None:
        chal = ContainerChallengeModel.query.filter(ContainerChallengeModel.image.isnot(None)).first()
        return chal.image if chal else None

    def _refresh_db_counts(self) -> None:
        """Refresh conservative physical load from shared database state."""

        contexts = DockerContextModel.query.all()
        counts_by_id = InstanceCoordinator.placement_counts(db.session)
        counts_by_name = {context.context_name: counts_by_id.get(context.id, 0) for context in contexts}

        with self.lock:
            self.container_counts = defaultdict(int, counts_by_name)
            self.weights = {context.context_name: int(context.weight) for context in contexts}
            self.health = {
                context.context_name: context.health_state == "healthy"
                for context in contexts
                if context.state == "active"
            }

    def load_from_db(self) -> None:
        # Preserve all configured endpoints. Draining/disabled/retired contexts
        # may still be required for cleanup, and down hosts must remain retryable.
        contexts = DockerContextModel.query.all()
        self.host_manager.load_contexts(contexts)
        connected = set(self.host_manager.get_connected_contexts())
        now = time.time()
        events = []

        for context in contexts:
            name = context.context_name
            is_connected = name in connected
            previous_health = context.health_state
            context.health_state = "healthy" if is_connected else "unhealthy"
            context.health_checked_at = now
            context.health_error = None if is_connected else "connection failed"

            if is_connected:
                meta: MetadataDict = {"context_name": name}
                image_info = self.host_manager.get_image_info(name, self._challenge_image())
                if image_info:
                    meta["image"] = {
                        "id": image_info["id"],
                        "size_mb": image_info["size_mb"],
                        "created": image_info["created"],
                    }
                if previous_health != "healthy":
                    events.append(("host_healthy", f"context {name} is healthy", "info", meta))
            elif previous_health != "unhealthy":
                events.append(
                    (
                        "host_unhealthy",
                        f"context {name} marked unhealthy: connection failed",
                        "warning",
                        {"context_name": name, "reason": "connection failed"},
                    )
                )

        db.session.commit()
        self._refresh_db_counts()

        for event_type, message, level, metadata in events:
            event_logger.log_event(event_type, message, level=level, metadata=metadata)

        healthy_count = sum(1 for context in contexts if context.health_state == "healthy")
        logger.info("loaded %d configured contexts, %d healthy", len(contexts), healthy_count)

    def mark_unhealthy(self, context_name: str, reason: str = "unreachable") -> None:
        context = DockerContextModel.query.filter_by(context_name=context_name).first()
        if context is not None:
            context.health_state = "unhealthy"
            context.health_checked_at = time.time()
            context.health_error = str(reason)[:512]
            db.session.commit()
        with self.lock:
            if context_name in self.health:
                self.health[context_name] = False
        logger.warning("context %s marked unhealthy: %s", context_name, reason)
        event_logger.log_event(
            "host_unhealthy",
            f"context {context_name} marked unhealthy: {reason}",
            level="warning",
            metadata={"context_name": context_name, "reason": reason},
        )

    def mark_healthy(self, context_name: str) -> None:
        context = DockerContextModel.query.filter_by(context_name=context_name).first()
        if context is not None:
            context.health_state = "healthy"
            context.health_checked_at = time.time()
            context.health_error = None
            db.session.commit()
        with self.lock:
            if context is not None and context.state == "active":
                self.health[context_name] = True
        logger.info("context %s marked healthy", context_name)
        event_logger.log_event(
            "host_healthy",
            f"context {context_name} marked healthy",
            level="info",
            metadata={"context_name": context_name},
        )

    def health_check(self) -> None:
        for name in self.host_manager.get_configured_contexts():
            probe_started = time.time()
            reachable = self.host_manager.ping(name)
            context = DockerContextModel.query.filter_by(context_name=name).first()
            if context is None:
                continue
            previous_state = context.health_state
            # A slower, older probe must not overwrite a newer result from a
            # second maintenance process during failover or misconfiguration.
            updated = DockerContextModel.query.filter(
                DockerContextModel.id == context.id,
                db.or_(
                    DockerContextModel.health_checked_at.is_(None),
                    DockerContextModel.health_checked_at < probe_started,
                ),
            ).update(
                {
                    DockerContextModel.health_state: "healthy" if reachable else "unhealthy",
                    DockerContextModel.health_checked_at: probe_started,
                    DockerContextModel.health_error: None if reachable else "connection failed",
                },
                synchronize_session=False,
            )
            db.session.commit()
            if updated != 1:
                continue
            with self.lock:
                if context.state == "active":
                    self.health[name] = reachable
            if reachable and previous_state != "healthy":
                logger.info("context %s marked healthy", name)
                event_logger.log_event(
                    "host_healthy",
                    f"context {name} marked healthy",
                    level="info",
                    metadata={"context_name": name},
                )
            elif not reachable and previous_state != "unhealthy":
                logger.warning("context %s marked unhealthy: connection failed", name)
                event_logger.log_event(
                    "host_unhealthy",
                    f"context {name} marked unhealthy: connection failed",
                    level="warning",
                    metadata={"context_name": name, "reason": "connection failed"},
                )

    def get_status(self) -> list[HostStatus]:
        self._refresh_db_counts()
        contexts = {context.context_name: context for context in DockerContextModel.query.all()}
        with self.lock:
            return [
                {
                    "context_name": name,
                    "pub_hostname": self.host_manager.get_pub_hostname(name),
                    "active_containers": self.container_counts.get(name, 0),
                    "healthy": context.state == "active" and context.health_state == "healthy",
                    "weight": int(context.weight or 1),
                    "score": int(context.weight or 1) / (self.container_counts.get(name, 0) + 1),
                }
                for name, context in sorted(contexts.items())
            ]
