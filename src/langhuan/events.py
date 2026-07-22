from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .config import Settings


def log_event(
    settings: Settings,
    event: str,
    summary: dict[str, Any],
    *,
    query: str | None = None,
) -> None:
    if not settings.log_events:
        return
    payload: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "summary": summary,
    }
    if query is not None:
        payload["query_length"] = len(query)
        if settings.include_content_in_events:
            payload["query"] = query
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with settings.event_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
