from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from threading import Lock

logger = logging.getLogger(__name__)

MetadataVal = str | int | float | bool | None
MetadataValue = MetadataVal | dict[str, MetadataVal | dict[str, MetadataVal]]
MetadataDict = dict[str, MetadataValue]

EventDict = dict[str, str | int | float | bool | None | MetadataDict]

UserFlagValues = tuple[bool, bool | None, bool | None]


def user_flag_values(user: object) -> UserFlagValues:
    is_admin = user.type == "admin" if user else False  # type: ignore[attr-defined]
    return is_admin, getattr(user, "hidden", False), getattr(user, "banned", False)


def flag_share_metadata(
    challenge_id: int | None,
    challenge_name: str | None,
    source_id: int | None,
    source_entity: str | None,
    source_type: str,
    team_id: int | None = None,
    team_name: str | None = None,
) -> dict:
    meta: dict = {
        "challenge_id": challenge_id,
        "challenge_name": challenge_name,
        "source_entity": source_entity,
        "source_id": source_id,
        "source_type": source_type,
    }
    if team_id is not None:
        meta["team_id"] = team_id
    if team_name is not None:
        meta["team_name"] = team_name
    return meta


def flag_share_message(submitter_name: str | None, source_entity: str | None, challenge_name: str | None) -> str:
    return f"user '{submitter_name}' submitted a flag belonging to '{source_entity}' on challenge '{challenge_name}'"


def dense_user_flags(values: UserFlagValues) -> dict[str, bool | None]:
    is_admin, is_hidden, is_banned = values
    return {"is_admin": is_admin, "is_hidden": is_hidden, "is_banned": is_banned}


def sparse_user_flags(values: UserFlagValues, out: dict | None = None) -> dict:
    is_admin, is_hidden, is_banned = values
    if out is None:
        out = {}
    if is_admin:
        out["is_admin"] = True
    if is_hidden:
        out["is_hidden"] = True
    if is_banned:
        out["is_banned"] = True
    return out


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

        user_flags: dict = {}
        if user_id:
            from CTFd.models import Users

            user = Users.query.filter_by(id=user_id).first()
            if user:
                if not username:
                    username = user.name
                sparse_user_flags(user_flag_values(user), user_flags)

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
