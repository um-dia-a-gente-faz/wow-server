"""Shared by every agent-runner module (#279): the error an action raises, secret
scrubbing and timestamp formatting. Nothing here does I/O."""

import os
import re
from datetime import datetime, timezone

SECRET_NAME = re.compile(r"PASSWORD|KEY|TOKEN|SECRET", re.I)


class RunnerError(Exception):
    """An action that failed in a way the caller should see (HTTP `status`)."""

    def __init__(self, message, status=502, detail=None, extra=None):
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.extra = extra or {}

    def body(self):
        return {"error": str(self), "detail": self.detail, **self.extra}


def scrub(text, extra=()):
    """Remove secret values (environment and `extra`) from text."""
    secrets = {v for k, v in os.environ.items() if SECRET_NAME.search(k) and len(v) >= 4}
    secrets.update(v for v in extra if v and len(v) >= 4)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
