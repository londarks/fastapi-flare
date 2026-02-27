"""
Alert notifiers for fastapi-flare.
====================================

Send HTTP webhook notifications when errors/warnings are captured.

Supported targets out of the box:
  - ``WebhookNotifier``   — generic HTTP POST with the raw entry as JSON
  - ``SlackNotifier``     — Incoming Webhook formatted message (Block Kit)
  - ``DiscordNotifier``   — Discord embed webhook
  - ``TeamsNotifier``     — Microsoft Teams Adaptive Card (new webhook format)

All notifiers are *fire-and-forget*: they run as background asyncio tasks
and silently swallow any exception so a delivery failure never impacts the
request path.

Cooldown / dedup is managed inside ``alerting.schedule_notifications`` based on
``FlareConfig.alert_cooldown_seconds``.

Usage::

    from fastapi_flare import setup, FlareConfig
    from fastapi_flare.notifiers import SlackNotifier, DiscordNotifier

    setup(app, config=FlareConfig(
        pg_dsn="postgresql://user:pass@localhost:5432/mydb",
        alert_notifiers=[
            SlackNotifier("https://hooks.slack.com/services/T00/B00/xxx"),
            DiscordNotifier("https://discord.com/api/webhooks/..."),
        ],
        alert_min_level="ERROR",       # "ERROR" | "WARNING"
        alert_cooldown_seconds=300,    # 5 min between alerts for the same fingerprint
    ))
"""
from __future__ import annotations

from typing import Any


class WebhookNotifier:
    """
    Generic HTTP POST notifier.

    Posts the full log entry dict as JSON to *url*.
    Use ``headers`` to pass authentication (e.g. ``{"Authorization": "Bearer …"}``).
    """

    def __init__(self, url: str, *, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers or {}

    def _build_payload(self, entry: dict) -> Any:
        """Override in subclasses to return a provider-specific payload."""
        return entry

    async def send(self, entry: dict) -> None:
        """
        Fire the webhook. Called as a background asyncio task.
        Never raises — all exceptions are silently swallowed.
        """
        try:
            import httpx

            async with httpx.AsyncClient(timeout=8.0) as client:
                await client.post(
                    self.url,
                    json=self._build_payload(entry),
                    headers=self.headers,
                )
        except Exception:  # noqa: BLE001
            pass


class SlackNotifier(WebhookNotifier):
    """
    Posts a Block Kit message to a Slack Incoming Webhook URL.

    Create a webhook at https://api.slack.com/apps → Incoming Webhooks.

    Example::

        SlackNotifier("https://hooks.slack.com/services/T00/B00/xxx")
    """

    def _build_payload(self, entry: dict) -> dict:
        level = entry.get("level", "ERROR")
        emoji = "🔴" if level == "ERROR" else "🟡"
        endpoint = entry.get("endpoint") or "unknown"
        message = entry.get("message") or ""
        ts = entry.get("timestamp", "")
        error = entry.get("error") or ""
        method = entry.get("http_method") or ""
        status = entry.get("http_status")

        header = f"{emoji} *{level}*"
        if method and status:
            header += f"  `{method} {endpoint}` → {status}"
        elif endpoint != "unknown":
            header += f"  `{endpoint}`"

        body = message
        if error and error != message:
            # Trim long stack traces to keep the Slack message readable
            short = error[:500] + ("…" if len(error) > 500 else "")
            body += f"\n```{short}```"

        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        ]
        if body.strip():
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": body}}
            )
        if ts:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"🕐 {ts}"}],
                }
            )

        return {"text": f"{emoji} {level} — {endpoint}", "blocks": blocks}


class DiscordNotifier(WebhookNotifier):
    """
    Posts an embed to a Discord webhook URL.

    Create a webhook in your server's channel settings → Integrations.

    Example::

        DiscordNotifier("https://discord.com/api/webhooks/123456/xxxx")
    """

    def _build_payload(self, entry: dict) -> dict:
        level = entry.get("level", "ERROR")
        color = 0xE53935 if level == "ERROR" else 0xFFB300  # red / amber
        endpoint = entry.get("endpoint") or "unknown"
        message = entry.get("message") or ""
        error = entry.get("error") or ""
        ts = entry.get("timestamp") or ""
        method = entry.get("http_method") or ""
        status = entry.get("http_status")

        title = f"{level}: {endpoint}"
        if method and status:
            title = f"{level}: {method} {endpoint} → {status}"

        description = message
        if error and error != message:
            short = error[:800] + ("…" if len(error) > 800 else "")
            description += f"\n```\n{short}\n```"

        embed: dict = {
            "title": title,
            "description": description,
            "color": color,
        }
        if ts:
            embed["timestamp"] = ts  # ISO-8601 — Discord renders it localised

        fields = []
        if entry.get("ip_address"):
            fields.append({"name": "IP", "value": entry["ip_address"], "inline": True})
        if entry.get("duration_ms") is not None:
            fields.append(
                {
                    "name": "Duration",
                    "value": f"{entry['duration_ms']} ms",
                    "inline": True,
                }
            )
        if fields:
            embed["fields"] = fields

        embed["footer"] = {"text": "fastapi-flare"}
        return {"embeds": [embed]}


class TeamsNotifier(WebhookNotifier):
    """
    Posts an Adaptive Card to a Microsoft Teams workflow webhook.

    The *new* Teams webhook format (Power Automate / Workflows).
    Create a flow: "Post to a channel when a webhook request is received".

    Example::

        TeamsNotifier("https://prod-xx.westus.logic.azure.com:443/workflows/...")
    """

    def _build_payload(self, entry: dict) -> dict:
        level = entry.get("level", "ERROR")
        endpoint = entry.get("endpoint") or "unknown"
        message = entry.get("message") or ""
        ts = entry.get("timestamp", "")
        method = entry.get("http_method") or ""
        status = entry.get("http_status")
        error = entry.get("error") or ""

        title_label = f"{level}: {endpoint}"
        if method and status:
            title_label = f"{level}: {method} {endpoint} → {status}"

        color = "attention" if level == "ERROR" else "warning"  # Adaptive Card color names

        body_items: list[dict] = [
            {
                "type": "TextBlock",
                "text": title_label,
                "weight": "Bolder",
                "color": color,
                "size": "Medium",
            },
            {
                "type": "TextBlock",
                "text": message,
                "wrap": True,
            },
        ]

        facts = []
        if ts:
            facts.append({"title": "Time", "value": ts})
        if entry.get("ip_address"):
            facts.append({"title": "IP", "value": entry["ip_address"]})
        if entry.get("duration_ms") is not None:
            facts.append({"title": "Duration", "value": f"{entry['duration_ms']} ms"})
        if error and error != message:
            short = error[:500] + ("…" if len(error) > 500 else "")
            body_items.append(
                {
                    "type": "TextBlock",
                    "text": f"```\n{short}\n```",
                    "wrap": True,
                    "fontType": "Monospace",
                    "size": "Small",
                }
            )
        if facts:
            body_items.append({"type": "FactSet", "facts": facts})

        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": body_items,
                    },
                }
            ],
        }
