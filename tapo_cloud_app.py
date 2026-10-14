#!/usr/bin/env python3
"""Cloud-only Tapo desktop web app.

The browser talks only to this local UI. The Python process talks to Tapo's
cloud API and keeps the cloud token server-side; it never connects to a home
LAN address, RTSP endpoint, Raspberry Pi, or Tailscale.

The app opens Tapo's CIPC cloud relay and converts the MPEG-TS live stream to
MJPEG for the local browser. It never connects to a camera LAN address.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import queue
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from tapo_cloud_probe import (
    LOGIN_URL,
    TapoCloud,
    clear_session,
    error_code,
    load_session,
    result_of,
    save_session,
)


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "cloud_web"
ICON_PATH = ROOT / "tapo-cloud-viewer.png"
PRODUCT_NAME = "Topo - desktop app for Tapo camera"


class CloudMediaWorker:
    """Keep one cloud relay and one FFmpeg decoder per camera."""

    def __init__(self, client: TapoCloud, base_url: str, device: dict):
        self.client = client
        self.base_url = base_url
        self.device = device
        self.subscribers: set[queue.Queue[bytes]] = set()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f"tapo-camera-{device.get('deviceId', '')[:8]}", daemon=True
        )
        self.last_error = ""
        self.thread.start()

    def subscribe(self) -> queue.Queue[bytes]:
        subscriber: queue.Queue[bytes] = queue.Queue(maxsize=2)
        with self.lock:
            self.subscribers.add(subscriber)
        self.wake.set()
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[bytes]) -> None:
        with self.lock:
            self.subscribers.discard(subscriber)

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def _broadcast(self, frame: bytes) -> None:
        with self.lock:
            subscribers = tuple(self.subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(frame)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(frame)
                except queue.Full:
                    pass

    def _run(self) -> None:
        while not self.stop.is_set():
            with self.lock:
                has_subscribers = bool(self.subscribers)
            if not has_subscribers:
                self.wake.wait(timeout=1)
                self.wake.clear()
                continue
            try:
                self._relay_once()
                self.last_error = ""
            except (OSError, RuntimeError, ssl.SSLError, EOFError) as exc:
                self.last_error = str(exc)
                if not self.stop.wait(2):
                    continue
            except Exception as exc:  # Keep a broken relay from killing the worker.
                self.last_error = f"unexpected media error: {exc}"
                if not self.stop.wait(2):
                    continue

    def _relay_once(self) -> None:
        device_id = str(self.device.get("deviceId") or "")
        if not device_id:
            raise RuntimeError("camera has no deviceId")
        self.client.discover_cipc_url(self.base_url)
        player_id = str(uuid.uuid4())
        request_param = {
            "deviceId": device_id,
            "dataTimeoutCount": 0,
            "cloudType": 2,
            "deviceType": self.device.get("deviceType") or "SMART.IPCAMERA",
            "playerId": player_id,
            "rootCaVer": "1",
            "streamType": 0,
            "preConnection": 0,
            "resolution": "HD",
        }
        response = self.client.post_cipc("/v2/relay/request", request_param, player_id)
        relay_info = result_of(response)
        if error_code(response) != 0 or not isinstance(relay_info, dict):
            raise RuntimeError(f"relay rejected: code={error_code(response)}")
        relay_url = str(relay_info.get("relayUrl") or "")
        relay_token = str(relay_info.get("relayToken") or relay_info.get("sessionToken") or "")
        if not relay_url or not relay_token:
            raise RuntimeError("incomplete relay response")
        parsed = urlsplit(relay_url if "://" in relay_url else "https://" + relay_url)
        host = parsed.hostname
        if not host:
            raise RuntimeError("relay has no hostname")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query + "&retryTime=0"
        else:
            path += "?retryTime=0"
        headers = {
            "Host": host,
            "User-Agent": "Client=TP-Link_Tapo_Android Android 3.21.114/1.3",
            "Keep-Relay": "3600",
            "Accept": "*/*",
            "Content-Type": "multipart/mixed;boundary=--client-stream-boundary--",
            "Content-Length": "9223372036854775807",
            "X-token": relay_token,
            # The Android relay client sends the camera/client model here;
            # it is not the operating-system name.
            "X-Client-Model": str(self.device.get("deviceModel") or "C200"),
            "X-Client-UUID": player_id,
            "X-Redirect-Times": "0",
            # PullMode.DIRECT_FORWARDING in the official app is the string 2.
            "X-Pull-Mode": "2",
            "X-Version": "2.0",
            "X-Arrive-Latency": "0",
            "Connection": "Keep-alive",
        }
        if relay_info.get("elbCookie"):
            headers["Cookie"] = str(relay_info["elbCookie"])
        raw_request = (
            f"POST {path} HTTP/1.1\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in headers.items())
            + "\r\n"
        ).encode("ascii")

        ffmpeg = None
        jpeg_thread = None
        try:
            with socket.create_connection((host, parsed.port or 443), timeout=15) as plain_socket:
                with self.client.verify_context.wrap_socket(
                    plain_socket, server_hostname=host
                ) as tls_socket:
                    # Do not leave the UI stuck on a half-open relay that
                    # answered HTTP but never attached its media producer.
                    # The standalone probe can need several seconds between
                    # the HTTP response headers and the first multipart
                    # boundary. Keep the relay alive long enough for that
                    # producer-side delay.
                    tls_socket.settimeout(20)
                    tls_socket.sendall(raw_request)
                    buffer = bytearray()
                    self._read_http_headers(tls_socket, buffer)
                    ffmpeg = subprocess.Popen(
                        [
                            self.client_ffmpeg,
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            # MPEG-TS is a live pipe. The default FFmpeg
                            # probe waits for EOF before emitting frames;
                            # bound probing so JPEGs are produced while the
                            # relay remains open.
                            "-analyzeduration",
                            "0",
                            "-probesize",
                            "32768",
                            "-f",
                            "mpegts",
                            "-i",
                            "pipe:0",
                            "-an",
                            "-vf",
                            "fps=10",
                            "-q:v",
                            "6",
                            "-f",
                            "image2pipe",
                            "-vcodec",
                            "mjpeg",
                            "pipe:1",
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        bufsize=0,
                    )
                    jpeg_thread = threading.Thread(
                        target=self._read_jpegs,
                        args=(ffmpeg,),
                        name="tapo-jpeg-reader",
                        daemon=True,
                    )
                    jpeg_thread.start()
                    tls_socket.settimeout(20)
                    while not self.stop.is_set():
                        boundary = self._read_line(tls_socket, buffer)
                        if not boundary:
                            continue
                        if boundary in (b"\r", b"\n"):
                            continue
                        # The response declares `--device-stream-boundary--`
                        # as the MIME boundary. Multipart framing adds its
                        # own two leading hyphens, so the wire line normally
                        # starts with four (`----device...`). The Android
                        # parser matches the boundary name by containment.
                        if b"--device-stream-boundary--" not in boundary:
                            continue
                        part_headers: dict[str, str] = {}
                        while True:
                            line = self._read_line(tls_socket, buffer)
                            if not line:
                                break
                            key, value = line.decode("latin1").split(":", 1)
                            part_headers[key.strip().lower()] = value.strip()
                        if part_headers.get("x-if-encrypt") == "1":
                            raise RuntimeError("relay video is encrypted: decoder is not available yet")
                        length = int(part_headers.get("content-length", "0"))
                        # `/v2/relay/request` is a video-only request. Some
                        # relay nodes have returned an incomplete or unusual
                        # Content-Type value, but the payload is still the
                        # MPEG-TS video stream. The length and encryption flag
                        # are the reliable framing signals here.
                        is_video = length > 0
                        if is_video and ffmpeg.stdin is not None:
                            self._pipe_exact(tls_socket, buffer, length, ffmpeg.stdin)
                        else:
                            self._read_exact(tls_socket, buffer, length)
        finally:
            if ffmpeg is not None and ffmpeg.stdin is not None:
                try:
                    ffmpeg.stdin.close()
                except OSError:
                    pass
            if ffmpeg is not None:
                try:
                    ffmpeg.terminate()
                    ffmpeg.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    ffmpeg.kill()
                if jpeg_thread is not None:
                    jpeg_thread.join(timeout=2)

    @staticmethod
    def _read_http_headers(sock: ssl.SSLSocket, buffer: bytearray) -> bytes:
        while b"\r\n\r\n" not in buffer and len(buffer) < 65536:
            chunk = sock.recv(8192)
            if not chunk:
                raise EOFError("relay closed while reading headers")
            buffer.extend(chunk)
        separator = buffer.find(b"\r\n\r\n")
        if separator < 0:
            raise RuntimeError("relay headers are too large")
        headers = bytes(buffer[:separator])
        del buffer[: separator + 4]
        if not headers.startswith(b"HTTP/1.1 200"):
            raise RuntimeError("invalid relay HTTP response")
        return headers

    @staticmethod
    def _read_line(sock: ssl.SSLSocket, buffer: bytearray) -> bytes:
        while b"\r\n" not in buffer:
            chunk = sock.recv(8192)
            if not chunk:
                raise EOFError("relay closed")
            buffer.extend(chunk)
        separator = buffer.find(b"\r\n")
        line = bytes(buffer[:separator])
        del buffer[: separator + 2]
        return line

    @staticmethod
    def _read_exact(sock: ssl.SSLSocket, buffer: bytearray, length: int) -> bytes:
        while len(buffer) < length:
            chunk = sock.recv(max(8192, length - len(buffer)))
            if not chunk:
                raise EOFError("relay closed during a video packet")
            buffer.extend(chunk)
        payload = bytes(buffer[:length])
        del buffer[:length]
        return payload

    @staticmethod
    def _pipe_exact(
        sock: ssl.SSLSocket,
        buffer: bytearray,
        length: int,
        output,
    ) -> None:
        """Forward a video part while it arrives instead of buffering its I-frame."""
        remaining = length
        while remaining:
            if not buffer:
                chunk = sock.recv(min(8192, remaining))
                if not chunk:
                    raise EOFError("relay closed during a video packet")
                buffer.extend(chunk)
            amount = min(remaining, len(buffer))
            output.write(bytes(buffer[:amount]))
            output.flush()
            del buffer[:amount]
            remaining -= amount

    def _read_jpegs(self, ffmpeg: subprocess.Popen) -> None:
        if ffmpeg.stdout is None:
            return
        buffer = bytearray()
        while True:
            read1 = getattr(ffmpeg.stdout, "read1", ffmpeg.stdout.read)
            chunk = read1(8192)
            if not chunk:
                return
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 2:
                        del buffer[:-2]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    break
                frame = bytes(buffer[start : end + 2])
                del buffer[: end + 2]
                self._broadcast(frame)

    @property
    def client_ffmpeg(self) -> str:
        return getattr(self.client, "ffmpeg", "ffmpeg")


class CloudAppState:
    STATUS_CACHE_TTL = 10.0

    def __init__(self, client: TapoCloud, base_url: str, devices: list[dict]):
        self.client = client
        self.base_url = base_url
        self.devices = devices
        self.workers: dict[int, CloudMediaWorker] = {}
        self.workers_lock = threading.Lock()
        self.privacy_lock = threading.Lock()
        self.privacy_states: dict[int, bool] = {}
        self.privacy_checked_at: dict[int, float] = {}
        self.ptz_lock = threading.Lock()
        self.ptz_states: dict[int, bool] = {}
        self.ptz_checked_at: dict[int, float] = {}

    def worker(self, index: int) -> CloudMediaWorker:
        with self.workers_lock:
            worker = self.workers.get(index)
            if worker is None:
                worker = CloudMediaWorker(self.client, self.base_url, self.devices[index])
                self.workers[index] = worker
            return worker

    def close(self) -> None:
        with self.workers_lock:
            workers = tuple(self.workers.values())
        for worker in workers:
            worker.close()
        self.client.close()

    def reset_worker(self, index: int) -> None:
        """Drop a relay that may have become stale after a camera state change."""
        with self.workers_lock:
            worker = self.workers.pop(index, None)
        if worker is not None:
            worker.close()

    def privacy_state(self, index: int) -> tuple[bool | None, str]:
        now = time.monotonic()
        with self.privacy_lock:
            cached = self.privacy_states.get(index)
            checked_at = self.privacy_checked_at.get(index, 0.0)
        if cached is not None and now - checked_at < self.STATUS_CACHE_TTL:
            return cached, ""
        try:
            enabled = self.client.get_privacy_mode(self.base_url, self.devices[index])
        except (RuntimeError, OSError) as exc:
            with self.privacy_lock:
                cached = self.privacy_states.get(index)
            return cached, str(exc)
        with self.privacy_lock:
            self.privacy_states[index] = enabled
            self.privacy_checked_at[index] = now
        return enabled, ""

    def set_privacy(self, index: int, enabled: bool) -> bool:
        state = self.client.set_privacy_mode(
            self.base_url, self.devices[index], enabled
        )
        with self.privacy_lock:
            self.privacy_states[index] = state
            self.privacy_checked_at[index] = time.monotonic()
        # Privacy mode stops the camera media producer. Releasing the old
        # relay is important when privacy is disabled again: otherwise the
        # next preview can attach to a relay that was opened before the lens
        # state changed and will never produce video again.
        self.reset_worker(index)
        return state

    def ptz_state(self, index: int) -> tuple[bool | None, str]:
        now = time.monotonic()
        with self.ptz_lock:
            cached = self.ptz_states.get(index)
            checked_at = self.ptz_checked_at.get(index, 0.0)
        if cached is not None and now - checked_at < self.STATUS_CACHE_TTL:
            return cached, ""
        try:
            supported = self.client.ptz_supported(
                self.base_url, self.devices[index]
            )
        except (RuntimeError, OSError) as exc:
            with self.ptz_lock:
                cached = self.ptz_states.get(index)
            return cached, str(exc)
        with self.ptz_lock:
            self.ptz_states[index] = supported
            self.ptz_checked_at[index] = now
        return supported, ""

    def move_camera(self, index: int, x: int, y: int) -> None:
        self.client.move_motor(self.base_url, self.devices[index], x, y)

    def public_camera(self, index: int) -> dict:
        device = self.devices[index]
        cloud_status = device.get("status")
        privacy, privacy_error = self.privacy_state(index)
        ptz, ptz_error = self.ptz_state(index)
        return {
            "id": index,
            "name": device.get("alias") or device.get("deviceName") or f"Camera {index + 1}",
            "model": device.get("deviceModel") or "<n/d>",
            "status": cloud_status,
            # The account list's status is not a live reachability check: the
            # official app can relay a camera that is reported as 0 here.
            "account_status": cloud_status,
            "cloud": True,
            "privacy": privacy,
            "privacy_error": privacy_error,
            "ptz": ptz,
            "ptz_error": ptz_error,
        }

    def public_cameras(self) -> list[dict]:
        return [self.public_camera(index) for index in range(len(self.devices))]


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class CloudAppContext:
    """Own the current login, pending MFA flow and authenticated app state."""

    def __init__(self, fresh_login: bool = False):
        self.lock = threading.RLock()
        self.state: CloudAppState | None = None
        self.pending_mfa: dict | None = None
        if fresh_login:
            clear_session()
        else:
            self._restore_session()

    def _restore_session(self) -> None:
        session = load_session()
        if not session:
            return
        client = TapoCloud()
        client.token = str(session.get("token") or "")
        base_url = str(session.get("base_url") or LOGIN_URL)
        if not client.token:
            return
        try:
            devices = client.list_cameras(base_url)
        except (RuntimeError, OSError):
            clear_session()
            return
        self._replace_state(CloudAppState(client, base_url, devices))
        print("Reused Tapo session without MFA.")

    def _replace_state(self, state: CloudAppState | None) -> None:
        with self.lock:
            old_state = self.state
            self.state = state
        if old_state is not None:
            old_state.close()

    def current_state(self) -> CloudAppState | None:
        with self.lock:
            return self.state

    def session_info(self) -> dict:
        with self.lock:
            return {
                "authenticated": self.state is not None,
                "mfa_pending": self.pending_mfa is not None,
                "mfa_types": list(self.pending_mfa.get("supported") or [])
                if self.pending_mfa
                else [],
            }

    def login(self, username: str, password: str) -> dict:
        username = username.strip()
        if not username or not password:
            raise ValueError("email and password are required")
        client = TapoCloud()
        base_url, result = client.login(username, password)
        process_id = result.get("MFAProcessId")
        if process_id:
            supported = [int(value) for value in (result.get("supportedMFATypes") or [2])]
            with self.lock:
                self.pending_mfa = {
                    "client": client,
                    "base_url": base_url,
                    "username": username,
                    "password": password,
                    "process_id": str(process_id),
                    "supported": supported,
                }
            return {"mfa_required": True, "mfa_types": supported}
        self._finish_login(client, base_url, result)
        return {"mfa_required": False, **self.session_info()}

    def request_mfa(self, mfa_type: int) -> dict:
        with self.lock:
            pending = self.pending_mfa
        if pending is None:
            raise RuntimeError("no pending MFA login")
        supported = pending["supported"]
        if mfa_type not in (1, 2) or mfa_type not in supported:
            raise ValueError("MFA method is not available")
        pending["client"].request_mfa(
            pending["base_url"], pending["username"], pending["password"], mfa_type
        )
        with self.lock:
            if self.pending_mfa is not None:
                self.pending_mfa["mfa_type"] = mfa_type
        return {"ok": True, "mfa_type": mfa_type}

    def verify_mfa(self, code: str) -> dict:
        with self.lock:
            pending = self.pending_mfa
        if pending is None:
            raise RuntimeError("no pending MFA login")
        mfa_type = pending.get("mfa_type")
        if mfa_type not in (1, 2):
            raise ValueError("request the MFA code first")
        result = pending["client"].verify_mfa(
            pending["base_url"],
            pending["username"],
            code.strip(),
            pending["process_id"],
            mfa_type,
        )
        self._finish_login(pending["client"], pending["base_url"], result)
        with self.lock:
            self.pending_mfa = None
        return {"ok": True, **self.session_info()}

    def _finish_login(self, client: TapoCloud, base_url: str, result: dict) -> None:
        token = result.get("token")
        if not token:
            raise RuntimeError("login response has no token")
        client.token = str(token)
        devices = client.list_cameras(base_url)
        save_session(base_url, result)
        self._replace_state(CloudAppState(client, base_url, devices))
        with self.lock:
            self.pending_mfa = None

    def logout(self) -> None:
        with self.lock:
            self.pending_mfa = None
        self._replace_state(None)
        clear_session()

    def close(self) -> None:
        self._replace_state(None)


class Handler(BaseHTTPRequestHandler):
    context: CloudAppContext

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def send_json(self, value: object, status: int = 200) -> None:
        body = json_bytes(value)
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > 4096:
            raise ValueError("invalid request body")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("invalid JSON")
        return value

    def require_state(self) -> CloudAppState | None:
        state = self.context.current_state()
        if state is None:
            self.send_json(
                {"ok": False, "authenticated": False, "error": "login required"},
                401,
            )
        return state

    def stream_mjpeg(self, state: CloudAppState, index: int) -> None:
        worker = state.worker(index)
        subscriber = worker.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                if self.context.current_state() is not state:
                    raise ConnectionError("session ended")
                try:
                    frame = subscriber.get(timeout=20)
                except queue.Empty:
                    if worker.last_error:
                        raise ConnectionError(worker.last_error)
                    continue
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode("ascii")
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionError, OSError):
            pass
        finally:
            worker.unsubscribe(subscriber)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        request_path = urlsplit(self.path).path
        if request_path == "/":
            self.send_file(WEB_ROOT / "index.html")
            return
        if request_path == "/assets/topo.png":
            icon_path = ICON_PATH if ICON_PATH.exists() else ROOT / "linux" / "tapo-cloud-viewer.png"
            self.send_file(icon_path)
            return
        if request_path == "/api/session":
            self.send_json(self.context.session_info())
            return
        if request_path == "/api/cameras":
            state = self.require_state()
            if state is not None:
                self.send_json({"cameras": state.public_cameras()})
            return
        if request_path.startswith("/api/cameras/"):
            raw_index = request_path[len("/api/cameras/") :].strip("/")
            if raw_index and "/" not in raw_index:
                state = self.require_state()
                if state is None:
                    return
                try:
                    index = int(raw_index)
                    camera = state.public_camera(index)
                except (ValueError, IndexError):
                    self.send_json({"ok": False, "error": "invalid camera"}, 404)
                    return
                self.send_json({"camera": camera})
                return
        if request_path.startswith("/stream/") and request_path.endswith(".mjpg"):
            state = self.require_state()
            if state is None:
                return
            raw_index = request_path[len("/stream/") : -len(".mjpg")].strip("/")
            try:
                index = int(raw_index)
                state.devices[index]
            except (ValueError, IndexError):
                self.send_json({"ok": False, "error": "invalid camera"}, 404)
                return
            self.stream_mjpeg(state, index)
            return
        if request_path.startswith("/api/cameras/") and request_path.endswith("/relay-check"):
            state = self.require_state()
            if state is None:
                return
            raw_index = request_path[len("/api/cameras/") : -len("/relay-check")].strip("/")
            try:
                index = int(raw_index)
                device = state.devices[index]
            except (ValueError, IndexError):
                self.send_json({"ok": False, "error": "invalid camera"}, 404)
                return
            try:
                relay_info = state.client.probe_cipc_preconnection(state.base_url, device)
                self.send_json({"ok": bool(relay_info), "relay": bool(relay_info)})
            except (RuntimeError, OSError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 502)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        request_path = urlsplit(self.path).path
        if request_path == "/api/login":
            try:
                payload = self.read_json_body()
                username = payload.get("username")
                password = payload.get("password")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError("email and password are required")
                self.send_json(self.context.login(username, password))
            except (RuntimeError, OSError, ValueError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 401)
            return
        if request_path == "/api/mfa/request":
            try:
                payload = self.read_json_body()
                mfa_type = payload.get("mfa_type")
                if isinstance(mfa_type, bool) or not isinstance(mfa_type, int):
                    raise ValueError("invalid MFA method")
                self.send_json(self.context.request_mfa(mfa_type))
            except (RuntimeError, OSError, ValueError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if request_path == "/api/mfa/verify":
            try:
                payload = self.read_json_body()
                code = payload.get("code")
                if not isinstance(code, str):
                    raise ValueError("MFA code is required")
                self.send_json(self.context.verify_mfa(code))
            except (RuntimeError, OSError, ValueError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 401)
            return
        if request_path == "/api/logout":
            self.context.logout()
            self.send_json({"ok": True, "authenticated": False})
            return
        prefix = "/api/cameras/"
        if not request_path.startswith(prefix):
            self.send_error(404)
            return
        state = self.require_state()
        if state is None:
            return
        if request_path.endswith("/privacy"):
            suffix = "/privacy"
            action = "privacy"
        elif request_path.endswith("/move"):
            suffix = "/move"
            action = "move"
        else:
            self.send_error(404)
            return
        raw_index = request_path[len(prefix) : -len(suffix)].strip("/")
        try:
            index = int(raw_index)
            state.devices[index]
            payload = self.read_json_body()
            if action == "privacy":
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
            else:
                x = payload.get("x", 0)
                y = payload.get("y", 0)
                if (
                    isinstance(x, bool)
                    or isinstance(y, bool)
                    or not isinstance(x, int)
                    or not isinstance(y, int)
                ):
                    raise ValueError("x and y must be integers")
                if not -200 <= x <= 200 or not -200 <= y <= 200:
                    raise ValueError("movement must be between -200 and 200")
        except (ValueError, IndexError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        try:
            if action == "privacy":
                privacy = state.set_privacy(index, enabled)
                self.send_json({"ok": True, "privacy": privacy})
            else:
                state.move_camera(index, x, y)
                self.send_json({"ok": True, "x": x, "y": y})
        except (RuntimeError, OSError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, 502)


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{PRODUCT_NAME} desktop app")
    parser.add_argument("--host", default="127.0.0.1", help="local UI address")
    parser.add_argument("--port", type=int, default=8790, help="local UI port")
    parser.add_argument("--fresh-login", action="store_true", help="force a new login/MFA")
    args = parser.parse_args()

    try:
        context = CloudAppContext(args.fresh_login)
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    handler_class = type("CloudAppHandler", (Handler,), {"context": context})
    server = ThreadingHTTPServer((args.host, args.port), handler_class)
    print(f"{PRODUCT_NAME} available at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop the app.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nApp stopped.")
    finally:
        context.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
