#!/usr/bin/env python3
"""Battery-friendly Tapo cloud alert poller for Topo.

Polls the Tapo notification center over HTTPS without opening a live relay.
When a doorbell/camera activity notification arrives, the desktop shell can
open a short preview stream and then tear it down again.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from tapo_cloud_probe import (
    LOGIN_URL,
    TapoCloud,
    activity_label,
    is_activity_notification,
    load_session,
)


AlertCallback = Callable[["WatchAlert"], None]


@dataclass(frozen=True)
class WatchAlert:
    """A matched camera/doorbell activity event."""

    msg_id: str
    time_ms: int
    msg_type: str
    label: str
    title: str
    device_index: int
    device_id: str
    device_name: str
    device_model: str


class AlertWatcher:
    """Idle cloud poller that never opens a media relay by itself."""

    def __init__(
        self,
        *,
        poll_interval: float = 3.0,
        lookback_ms: int = 120_000,
        on_alert: AlertCallback | None = None,
        device_refresh_s: float = 300.0,
    ) -> None:
        self.poll_interval = max(1.0, float(poll_interval))
        self.lookback_ms = max(10_000, int(lookback_ms))
        self.on_alert = on_alert
        self.device_refresh_s = max(30.0, float(device_refresh_s))
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: TapoCloud | None = None
        self._base_url = LOGIN_URL
        self._devices: list[dict] = []
        self._devices_by_id: dict[str, tuple[int, dict]] = {}
        self._seen_ids: set[str] = set()
        self._since_ms = 0
        self._last_device_refresh = 0.0
        self._lock = threading.RLock()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="topo-alert-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        self._close_client()

    def refresh_devices(self) -> list[dict]:
        client, base_url = self._ensure_client()
        devices = client.list_cameras(base_url)
        by_id: dict[str, tuple[int, dict]] = {}
        for index, device in enumerate(devices):
            device_id = str(device.get("deviceId") or "")
            if device_id:
                by_id[device_id] = (index, device)
        with self._lock:
            self._devices = devices
            self._devices_by_id = by_id
            self._last_device_refresh = time.monotonic()
        return devices

    def poll_once(self, *, emit: bool = True) -> list[WatchAlert]:
        """Fetch new notifications and optionally return matched alerts.

        When emit is False, notifications are only marked as seen. Used on
        startup so the lookback window does not replay old events as popups.
        """
        if self.paused and emit:
            return []
        now = time.monotonic()
        with self._lock:
            need_refresh = (
                not self._devices
                or now - self._last_device_refresh >= self.device_refresh_s
            )
        if need_refresh:
            self.refresh_devices()

        client, base_url = self._ensure_client()
        with self._lock:
            since_ms = self._since_ms
            if since_ms <= 0:
                since_ms = int(time.time() * 1000) - self.lookback_ms
            devices = list(self._devices)
            by_id = dict(self._devices_by_id)

        notifications = client.list_app_notifications(base_url, since_ms=since_ms)
        alerts: list[WatchAlert] = []
        newest_ms = since_ms
        for notification in notifications:
            time_ms = int(notification.get("time_ms") or 0)
            if time_ms > newest_ms:
                newest_ms = time_ms
            msg_id = str(notification.get("msg_id") or "")
            if not msg_id or msg_id in self._seen_ids:
                continue
            self._seen_ids.add(msg_id)
            if not emit or not is_activity_notification(notification):
                continue
            matched = self._match_device(notification, devices, by_id)
            if matched is None:
                continue
            index, device = matched
            alerts.append(
                WatchAlert(
                    msg_id=msg_id,
                    time_ms=time_ms,
                    msg_type=str(notification.get("msg_type") or ""),
                    label=activity_label(notification),
                    title=str(notification.get("title") or ""),
                    device_index=index,
                    device_id=str(device.get("deviceId") or ""),
                    device_name=str(
                        device.get("alias")
                        or device.get("deviceName")
                        or notification.get("device_name")
                        or f"Camera {index + 1}"
                    ),
                    device_model=str(device.get("deviceModel") or ""),
                )
            )

        # Keep the cursor just behind the newest event so the next poll is
        # incremental, while still re-including the latest second if clocks skew.
        with self._lock:
            self._since_ms = max(since_ms, newest_ms - 1000)
            if len(self._seen_ids) > 2000:
                # Bound memory; IDs older than the cursor are no longer needed.
                self._seen_ids = set(list(self._seen_ids)[-1000:])
        return alerts

    def _match_device(
        self,
        notification: dict,
        devices: list[dict],
        by_id: dict[str, tuple[int, dict]],
    ) -> tuple[int, dict] | None:
        device_id = str(notification.get("device_id") or "")
        if device_id and device_id in by_id:
            return by_id[device_id]

        needle = " ".join(
            str(notification.get(key) or "").lower()
            for key in ("device_name", "title", "content")
        )
        if not needle:
            # Single-device accounts: still useful to surface the only camera.
            if len(devices) == 1:
                return 0, devices[0]
            return None

        for index, device in enumerate(devices):
            names = [
                str(device.get("alias") or ""),
                str(device.get("deviceName") or ""),
                str(device.get("deviceModel") or ""),
            ]
            for name in names:
                text = name.strip().lower()
                if text and text in needle:
                    return index, device
        return None

    def _ensure_client(self) -> tuple[TapoCloud, str]:
        with self._lock:
            if self._client is not None:
                return self._client, self._base_url
        session = load_session()
        if not session or not session.get("token"):
            raise RuntimeError(
                "no saved Tapo session; open Topo once and log in first"
            )
        client = TapoCloud()
        client.token = str(session.get("token") or "")
        base_url = str(session.get("base_url") or LOGIN_URL)
        # Validate the session early so the tray can show a clear error.
        client.list_cameras(base_url)
        with self._lock:
            self._client = client
            self._base_url = base_url
        return client, base_url

    def _close_client(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            client.close()

    def _run(self) -> None:
        # Seed seen IDs with the recent window so restarting the watcher does
        # not replay old alerts as brand-new popups.
        try:
            self.poll_once(emit=False)
        except Exception as exc:  # noqa: BLE001 - keep watcher alive
            print(f"Topo watcher startup poll failed: {exc}", flush=True)

        while not self._stop.wait(self.poll_interval):
            if self.paused:
                continue
            try:
                alerts = self.poll_once(emit=True)
            except Exception as exc:  # noqa: BLE001 - transient cloud errors
                print(f"Topo watcher poll failed: {exc}", flush=True)
                self._close_client()
                continue
            callback = self.on_alert
            if callback is None:
                continue
            for alert in alerts:
                try:
                    callback(alert)
                except Exception as exc:  # noqa: BLE001
                    print(f"Topo watcher alert callback failed: {exc}", flush=True)


def describe_alert(alert: WatchAlert) -> str:
    model = alert.device_model or "Camera"
    return f"{alert.device_name} · {alert.label} ({model})"
