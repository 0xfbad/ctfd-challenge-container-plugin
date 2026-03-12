import time
from threading import Lock
from collections import deque


class EventLogger:
    def __init__(self, max_events=500):
        self.events = deque(maxlen=max_events)
        self.lock = Lock()
        self.listeners = []

    def log_event(
        self,
        event_type,
        container_id=None,
        challenge_id=None,
        challenge_name=None,
        user_id=None,
        user_name=None,
        team_id=None,
        team_name=None,
        message=None,
    ):
        event = {
            "timestamp": int(time.time()),
            "type": event_type,
            "container_id": container_id,
            "challenge_id": challenge_id,
            "challenge": challenge_name,
            "user_id": user_id,
            "user": user_name,
            "team_id": team_id,
            "team": team_name,
            "message": message,
        }

        with self.lock:
            self.events.append(event)

            for listener in self.listeners[:]:
                try:
                    listener(event)
                except Exception:
                    self.listeners.remove(listener)

        return event

    def get_recent_events(self, limit=50):
        with self.lock:
            events_list = list(self.events)
            return events_list[-limit:] if limit else events_list

    def add_listener(self, callback):
        with self.lock:
            self.listeners.append(callback)

    def remove_listener(self, callback):
        with self.lock:
            if callback in self.listeners:
                self.listeners.remove(callback)


event_logger = EventLogger()
