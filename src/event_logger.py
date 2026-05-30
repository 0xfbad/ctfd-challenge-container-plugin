from __future__ import annotations

import time
import logging
from collections.abc import Callable
from threading import Lock
from collections import deque
from datetime import datetime

logger = logging.getLogger(__name__)

# scalar types allowed in metadata values
MetadataVal = str | int | float | bool | None

# metadata can contain nested dicts (e.g. ImageInfo, host scores)
MetadataDict = dict[str, MetadataVal | dict[str, MetadataVal | dict[str, MetadataVal]]]

# shape of event dicts produced by EventLogger.log_event
EventDict = dict[str, str | int | float | bool | None | MetadataDict]


class EventLogger:
    def __init__(self, max_events: int = 2000) -> None:
        self.events: deque[EventDict] = deque(maxlen=max_events)
        self.lock = Lock()
        self.listeners: list[Callable[[EventDict], None]] = []
        self._next_id: int = 1

    def log_event(
        self,
        event_type: str,
        message: str,
        user_id: int | None = None,
        username: str | None = None,
        level: str = "info",
        metadata: MetadataDict | None = None,
    ) -> EventDict:
        from . import event_bus

        with self.lock:
            event_id = f"{event_bus.WORKER_ID}:{self._next_id}"
            self._next_id += 1

        user_flags = {}
        if user_id:
            from CTFd.models import Users

            user = Users.query.filter_by(id=user_id).first()
            if user:
                if not username:
                    username = user.name
                if user.type == "admin":
                    user_flags["is_admin"] = True
                if getattr(user, "hidden", False):
                    user_flags["is_hidden"] = True
                if getattr(user, "banned", False):
                    user_flags["is_banned"] = True

        event = {
            "id": event_id,
            "timestamp": time.time(),
            "datetime": datetime.now().strftime("%b %-d, %Y %-I:%M:%S %p"),
            "type": event_type,
            "level": level,
            "message": message,
            "user_id": user_id,
            "username": username,
            **user_flags,
            "metadata": metadata or {},
        }

        self._deliver_local(event)

        try:
            from . import event_bus

            event_bus.publish(event)
        except Exception:
            logger.warning("event bus publish failed", exc_info=True)

        log_msg = f"[{event_type}] {message}"
        if username:
            log_msg = f"[{event_type}] User {username} (ID: {user_id}): {message}"

        if level == "error":
            logger.error(log_msg)
        elif level == "warning":
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

        return event

    def _deliver_local(self, event: EventDict) -> None:
        with self.lock:
            self.events.append(event)
            listeners = self.listeners[:]

        failed = []
        for listener in listeners:
            try:
                listener(event)
            except Exception as e:
                logger.warning(f"event listener failed and was removed: {str(e)}")
                failed.append(listener)

        if failed:
            with self.lock:
                for listener in failed:
                    if listener in self.listeners:
                        self.listeners.remove(listener)

    def get_recent_events(self, limit: int = 100) -> list[EventDict]:
        with self.lock:
            events_list = list(self.events)
            return events_list[-limit:] if limit else events_list

    def add_listener(self, callback: Callable[[EventDict], None]) -> None:
        with self.lock:
            self.listeners.append(callback)

    def remove_listener(self, callback: Callable[[EventDict], None]) -> None:
        with self.lock:
            if callback in self.listeners:
                self.listeners.remove(callback)


event_logger = EventLogger()
