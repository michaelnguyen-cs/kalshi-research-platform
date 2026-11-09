"""
Outbound alerts — Discord, Telegram, email.

All methods are fire-and-forget with a logged warning on failure.
Configure via .env:
  DISCORD_WEBHOOK_URL
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  ALERT_EMAIL  (not implemented; placeholder for SMTP integration)
"""

from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

import httpx
import structlog

from kbot.config import Settings, settings as _default_settings

log = structlog.get_logger(__name__)


class Alerter:
    """
    Sends text alerts to configured channels.

    All methods are synchronous; call from a thread or use asyncio.to_thread()
    if you need them from async code.
    """

    def __init__(self, cfg: Settings | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._http = httpx.Client(timeout=10.0)

    def send(self, message: str) -> None:
        """Send `message` to every configured alert channel."""
        if self._cfg.discord_webhook_url:
            self._discord(message)
        if self._cfg.telegram_bot_token and self._cfg.telegram_chat_id:
            self._telegram(message)

    def send_critical(self, message: str) -> None:
        """Send a critical-priority alert (prefixed with [CRITICAL]) to all channels."""
        self.send(f"[CRITICAL] {message}")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Alerter":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── Channels ──────────────────────────────────────────────────────────────

    def _discord(self, message: str) -> None:
        try:
            resp = self._http.post(
                self._cfg.discord_webhook_url,
                json={"content": message[:2000]},  # Discord 2000-char limit
            )
            resp.raise_for_status()
            log.debug("alert.discord_sent")
        except Exception as exc:  # noqa: BLE001
            log.warning("alert.discord_failed", error=str(exc)[:200])

    def _telegram(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self._cfg.telegram_bot_token}/sendMessage"
        try:
            resp = self._http.post(url, json={
                "chat_id": self._cfg.telegram_chat_id,
                "text": message[:4096],  # Telegram 4096-char limit
            })
            resp.raise_for_status()
            log.debug("alert.telegram_sent")
        except Exception as exc:  # noqa: BLE001
            log.warning("alert.telegram_failed", error=str(exc)[:200])
