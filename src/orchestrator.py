from __future__ import annotations

import logging
from threading import Lock
from collections import defaultdict
from typing import TypedDict

from .docker_host_manager import DockerHostManager
from .models import ContainerChallengeModel, DockerContextModel
from .utils import get_setting
from .event_logger import MetadataDict, event_logger

logger = logging.getLogger(__name__)


class HostStatus(TypedDict):
    context_name: str
    pub_hostname: str | None
    active_containers: int
    healthy: bool
    weight: int
    score: float


class Orchestrator:
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

    def load_from_db(self) -> None:
        contexts = DockerContextModel.query.filter_by(enabled=True).all()

        max_concurrent = int(get_setting("max_concurrent_creates", 2) or 2)
        self.host_manager.load_contexts(contexts, max_concurrent)
        connected = set(self.host_manager.get_connected_contexts())

        new_health = {}
        new_weights = {}
        events = []

        for ctx in contexts:
            name = ctx.context_name
            is_connected = name in connected
            new_health[name] = is_connected
            new_weights[name] = ctx.weight

            if is_connected:
                meta: MetadataDict = {"context_name": name}
                image_info = self.host_manager.get_image_info(name, self._challenge_image())
                if image_info:
                    meta["image"] = {
                        "id": image_info["id"],
                        "size_mb": image_info["size_mb"],
                        "created": image_info["created"],
                    }
                events.append(("host_healthy", f"context {name} is healthy", "info", meta))
            else:
                events.append(
                    (
                        "host_unhealthy",
                        f"context {name} marked unhealthy: connection failed",
                        "warning",
                        {"context_name": name, "reason": "connection failed"},
                    )
                )

        known = {ctx.context_name for ctx in contexts}

        with self.lock:
            self.health = new_health
            self.weights = new_weights
            for name in list(self.container_counts.keys()):
                if name not in known:
                    del self.container_counts[name]
            for name in known:
                if name not in self.container_counts:
                    self.container_counts[name] = 0

        for event_type, message, level, metadata in events:
            event_logger.log_event(event_type, message, level=level, metadata=metadata)

        healthy_count = sum(1 for h in new_health.values() if h)
        logger.info(f"loaded {len(contexts)} contexts, {healthy_count} healthy")

    def _score_locked(self, name: str) -> float:
        # load-balancer score; caller must hold self.lock. authoritative for placement
        # in _pick_best_context and surfaced via get_status for observability
        count = self.container_counts[name]
        weight = self.weights.get(name, 1)
        return weight / (count + 1)

    def _pick_best_context(self) -> str | None:
        # caller must hold self.lock
        candidates = []
        for name, healthy in self.health.items():
            if not healthy:
                continue
            candidates.append((self._score_locked(name), name))

        if not candidates:
            return None

        candidates.sort(key=lambda x: (-x[0], x[1]))
        return candidates[0][1]

    def select_and_reserve(self) -> str | None:
        with self.lock:
            name = self._pick_best_context()
            if name is None:
                return None
            self.container_counts[name] += 1
            return name

    def reserve_slot(self, context_name: str) -> None:
        with self.lock:
            self.container_counts[context_name] += 1

    def release_slot(self, context_name: str) -> None:
        with self.lock:
            if self.container_counts[context_name] > 0:
                self.container_counts[context_name] -= 1

    def mark_unhealthy(self, context_name: str, reason: str = "unreachable") -> None:
        with self.lock:
            self.health[context_name] = False
        logger.warning(f"context {context_name} marked unhealthy: {reason}")
        event_logger.log_event(
            "host_unhealthy",
            f"context {context_name} marked unhealthy: {reason}",
            level="warning",
            metadata={"context_name": context_name, "reason": reason},
        )

    def mark_healthy(self, context_name: str) -> None:
        with self.lock:
            self.health[context_name] = True
        logger.info(f"context {context_name} marked healthy")
        event_logger.log_event(
            "host_healthy",
            f"context {context_name} marked healthy",
            level="info",
            metadata={"context_name": context_name},
        )

    def health_check(self) -> None:
        with self.lock:
            names = list(self.health.keys())

        for name in names:
            reachable = self.host_manager.ping(name)
            with self.lock:
                was_healthy = self.health.get(name)

            if reachable and not was_healthy:
                self.mark_healthy(name)
            elif not reachable and was_healthy:
                self.mark_unhealthy(name)

    def get_status(self) -> list[HostStatus]:
        with self.lock:
            status: list[HostStatus] = []
            for name in self.health:
                status.append(
                    {
                        "context_name": name,
                        "pub_hostname": self.host_manager.get_pub_hostname(name),
                        "active_containers": self.container_counts.get(name, 0),
                        "healthy": self.health[name],
                        "weight": self.weights.get(name, 1),
                        "score": self._score_locked(name),
                    }
                )
            return status
