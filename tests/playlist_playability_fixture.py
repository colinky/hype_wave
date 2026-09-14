"""Healthy, fresh offline observations for stateful playlist mutation fixtures."""
from datetime import datetime, timedelta, timezone


class PlaylistVerifier:
    environment = "offline-playlist"

    def __init__(self, states=None, *, health="healthy", metadata=None):
        self.states = states or {}
        self.health = health
        self.metadata = metadata or {}
        self.calls = []

    def check_health(self, **kwargs):
        return {"run_health": self.health, "auth_state": "authenticated" if self.health == "healthy" else "unknown"}

    def verify(self, video_id, *, availability=None, **kwargs):
        now = datetime.now(timezone.utc)
        state = self.states.get(video_id, "playable")
        if availability is False and state == "playable":
            state = "unknown"
        if availability is True and state == "unavailable":
            state = "unknown"
        self.calls.append((video_id, availability))
        return {**self.metadata.get(video_id, {}), "video_id": video_id, "state": state, "auth_state": "authenticated", "run_health": self.health,
                "exact_id": True, "reason_code": "offline", "has_audio": state == "playable",
                "observed_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "environment": self.environment}
