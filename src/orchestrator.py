import logging
from threading import Lock
from collections import defaultdict

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, host_manager):
        self.host_manager = host_manager
        self.container_counts = defaultdict(int)
        self.health = {}
        self.weights = {}
        self.lock = Lock()

    def load_from_db(self):
        from .models import DockerContextModel
        from .utils import get_setting
        from .event_logger import event_logger

        try:
            contexts = DockerContextModel.query.filter_by(enabled=True).all()
        except Exception as e:
            logger.error(f"could not query docker contexts: {e}")
            contexts = []

        max_concurrent = get_setting("max_concurrent_creates", 2)
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
                events.append(("host_healthy", f"context {name} is healthy", "info", {"context_name": name}))
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

    def _pick_best_context(self):
        """Must be called with self.lock held."""
        candidates = []
        for name, healthy in self.health.items():
            if not healthy:
                continue
            count = self.container_counts[name]
            weight = self.weights.get(name, 1)
            score = weight / (count + 1)
            candidates.append((score, name))

        if not candidates:
            return None

        candidates.sort(key=lambda x: (-x[0], x[1]))
        return candidates[0][1]

    def select_and_reserve(self):
        """Pick the best context and increment its count atomically."""
        with self.lock:
            name = self._pick_best_context()
            if name is None:
                return None
            self.container_counts[name] += 1
            return name

    def reserve_slot(self, context_name):
        with self.lock:
            self.container_counts[context_name] += 1

    def release_slot(self, context_name):
        with self.lock:
            if self.container_counts[context_name] > 0:
                self.container_counts[context_name] -= 1

    def mark_unhealthy(self, context_name, reason="unreachable"):
        from .event_logger import event_logger

        with self.lock:
            self.health[context_name] = False
        logger.warning(f"context {context_name} marked unhealthy: {reason}")
        event_logger.log_event(
            "host_unhealthy",
            f"context {context_name} marked unhealthy: {reason}",
            level="warning",
            metadata={"context_name": context_name, "reason": reason},
        )

    def mark_healthy(self, context_name):
        from .event_logger import event_logger

        with self.lock:
            self.health[context_name] = True
        logger.info(f"context {context_name} marked healthy")
        event_logger.log_event(
            "host_healthy",
            f"context {context_name} marked healthy",
            level="info",
            metadata={"context_name": context_name},
        )

    def health_check(self):
        """Ping each context and update health status."""
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

    def get_status(self):
        with self.lock:
            status = []
            for name in self.health:
                status.append(
                    {
                        "context_name": name,
                        "pub_hostname": self.host_manager.get_pub_hostname(name),
                        "active_containers": self.container_counts.get(name, 0),
                        "healthy": self.health[name],
                        "weight": self.weights.get(name, 1),
                    }
                )
            return status
