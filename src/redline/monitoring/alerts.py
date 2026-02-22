"""Slack alert manager."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from redline.logging import RunLogger

# Events that must trigger a Slack alert when webhook is configured.
ALERT_EVENTS = frozenset(
    {"HALT_TRIGGERED", "REBALANCE_COMPLETED", "REBALANCE_SKIPPED", "RISK_CIRCUIT_BREAKER"}
)


class AlertManager:
    def __init__(
        self,
        webhook_url: str | None,
        run_id: str,
        logger: RunLogger,
        env: str = "dev",
    ) -> None:
        self._webhook = webhook_url
        self._run_id = run_id
        self._logger = logger
        self._env = env
        self._sent: set[str] = set()
        self._disabled_logged = False

    def send_alert(self, event_type: str, data: dict[str, Any], dry_run: bool = True) -> None:
        """Send a Slack alert, or log ALERT_DISABLED once if webhook is not configured.

        Rate-limited: at most one alert per event type per run_id.
        """
        if event_type in self._sent:
            return
        self._sent.add(event_type)

        if not self._webhook:
            if not self._disabled_logged:
                self._logger.info("ALERT_DISABLED", run_id=self._run_id)
                self._disabled_logged = True
            return

        msg = _format_message(event_type, self._run_id, data, self._env, dry_run)
        try:
            resp = httpx.post(self._webhook, json={"text": msg}, timeout=10.0)
            resp.raise_for_status()
            self._logger.info("slack_alert_sent", event_type=event_type)
        except Exception as exc:
            self._logger.warning("slack_alert_failed", event_type=event_type, error=str(exc))


def _format_message(
    event_type: str,
    run_id: str,
    data: dict[str, Any],
    env: str,
    dry_run: bool,
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"redline: {event_type} ({env})",
        f"• ts: {ts}",
        f"• run_id: {run_id}",
        f"• dry_run: {dry_run}",
    ]
    for k, v in data.items():
        lines.append(f"• {k}: {v}")
    return "\n".join(lines)
