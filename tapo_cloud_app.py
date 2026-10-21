#!/usr/bin/env python3
"""Cloud-only Tapo desktop web app.

The browser talks only to this local UI. The Python process talks to Tapo's
cloud API and keeps the cloud token server-side; it never connects to a home
LAN address, RTSP endpoint, Raspberry Pi, or Tailscale.

The app opens Tapo's CIPC cloud relay and exposes both a browser-compatible
MJPEG path and a low-latency fragmented-MP4 path for the local browser. It
never connects to a camera LAN address.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import mimetypes
import os
import queue
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tapo_cloud_probe import (
    LOGIN_URL,
    TapoCloud,
    clear_session,
    error_code,
    load_session,
    reset_terminal_uuid,
    result_of,
    save_session,
)


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WEB_ROOT = ROOT / "cloud_web"
ICON_PATH = ROOT / "tapo-cloud-viewer.png"
if not ICON_PATH.exists():
    ICON_PATH = ROOT / "io.github.filirnd.topo.png"
PRODUCT_NAME = "Topo - desktop app for Tapo camera"
STREAM_START_TIMEOUT = 12.0
STREAM_RESOLUTION = os.environ.get("TOPO_STREAM_RESOLUTION", "HD").strip().upper()
if STREAM_RESOLUTION not in {"HD", "SD"}:
    STREAM_RESOLUTION = "HD"


def native_player_path() -> Path | None:
    """Return the bundled native decoder, when this build includes one."""
    override = os.environ.get("TOPO_NATIVE_PLAYER")
    candidates = [Path(override)] if override else []
    candidates.extend(
        [
            ROOT / ("native-ffmpeg-player.exe" if os.name == "nt" else "native-ffmpeg-player"),
            ROOT / ("ffplay.exe" if os.name == "nt" else "ffplay"),
            ROOT / "experiments" / "native-ffmpeg-player" / (
                "native-ffmpeg-player.exe" if os.name == "nt" else "native-ffmpeg-player"
            ),
        ]
    )
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    system_ffplay = shutil.which("ffplay")
    if system_ffplay:
        return Path(system_ffplay)
    return None


def ffmpeg_command_available() -> bool:
    command = os.environ.get("TOPO_FFMPEG") or "ffmpeg"
    return Path(command).is_file() or shutil.which(command) is not None


def native_player_command(player_path: Path, title: str) -> list[str]:
    """Build the stdin MPEG-TS command for the bundled player or ffplay."""
    window_title = f"Topo - low-latency player - {title}"
    if player_path.name.lower() in {"ffplay", "ffplay.exe"}:
        return [
            str(player_path),
            "-window_title",
            window_title,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-framedrop",
            "-sync",
            "video",
            "-probesize",
            "32768",
            "-analyzeduration",
            "0",
            "-f",
            "mpegts",
            "-i",
            "pipe:0",
        ]
    return [str(player_path), "--title", window_title]


class _MulawTsExtractor:
    """Extract private G.711 μ-law PES payloads from a live MPEG-TS stream."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.audio_pids: set[int] = set()
        self.remaining_by_pid: dict[int, int] = {}

    def feed(self, data: bytes) -> bytes:
        self.buffer.extend(data)
        audio = bytearray()
        while len(self.buffer) >= 188:
            if self.buffer[0] != 0x47:
                try:
                    next_sync = self.buffer.index(0x47)
                except ValueError:
                    del self.buffer[:-187]
                    break
                del self.buffer[:next_sync]
                if len(self.buffer) < 188:
                    break
            packet = bytes(self.buffer[:188])
            del self.buffer[:188]
            extracted = self._packet(packet)
            if extracted:
                audio.extend(extracted)
        return bytes(audio)

    def _packet(self, packet: bytes) -> bytes:
        if packet[0] != 0x47:
            return b""
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        adaptation = (packet[3] >> 4) & 0x3
        if not adaptation & 1:
            return b""
        payload_offset = 4
        if adaptation & 2:
            payload_offset += 1 + packet[4]
        if payload_offset >= 188:
            return b""
        payload = packet[payload_offset:]
        if packet[1] & 0x40:
            self.remaining_by_pid[pid] = 0
            if len(payload) < 14 or payload[:3] != b"\x00\x00\x01":
                return b""
            stream_id = payload[3]
            if not 0xC0 <= stream_id <= 0xDF:
                return b""
            self.audio_pids.add(pid)
            packet_length = int.from_bytes(payload[4:6], "big")
            header_length = 9 + payload[8]
            if packet_length <= 0 or header_length > 6 + packet_length:
                return b""
            self.remaining_by_pid[pid] = 6 + packet_length - header_length
            payload = payload[header_length:]
        if pid not in self.audio_pids:
            return b""
        remaining = self.remaining_by_pid.get(pid, 0)
        if remaining <= 0:
            return b""
        take = min(len(payload), remaining)
        self.remaining_by_pid[pid] = remaining - take
        return payload[:take]


class CloudMediaWorker:
    """Keep one cloud relay and one FFmpeg decoder per camera."""

    def __init__(self, client: TapoCloud, base_url: str, device: dict):
        self.client = client
        self.base_url = base_url
        self.device = device
        self.subscribers: set[queue.Queue[bytes]] = set()
        self.audio_subscribers: set[queue.Queue[bytes]] = set()
        self.native_video_subscribers: set[queue.Queue[bytes]] = set()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f"tapo-camera-{device.get('deviceId', '')[:8]}", daemon=True
        )
        self.last_error = ""
        self.native_lock = threading.RLock()
        self.relay_lock = threading.Lock()
        self.relay_socket: ssl.SSLSocket | None = None
        self.native_requested = False
        self.native_process: subprocess.Popen | None = None
        self.native_video_running = False
        self.native_init_segment = b""
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

    def subscribe_audio(self) -> queue.Queue[bytes]:
        subscriber: queue.Queue[bytes] = queue.Queue(maxsize=32)
        with self.lock:
            self.audio_subscribers.add(subscriber)
        self.wake.set()
        return subscriber

    def unsubscribe_audio(self, subscriber: queue.Queue[bytes]) -> None:
        with self.lock:
            self.audio_subscribers.discard(subscriber)

    def subscribe_native_video(self) -> queue.Queue[bytes]:
        subscriber: queue.Queue[bytes] = queue.Queue()
        with self.lock:
            self.native_video_subscribers.add(subscriber)
            native_video_running = self.native_video_running
            init_segment = self.native_init_segment
            if not native_video_running:
                # A fresh remuxer must produce a new init segment. Do not give
                # a new subscriber an init header belonging to an old relay.
                self.native_init_segment = b""
                init_segment = b""
        if init_segment:
            subscriber.put_nowait(init_segment)
        self.last_error = ""
        # The remuxer needs the beginning of a fresh MPEG-TS stream so it can
        # copy the H.264 configuration into the fragmented MP4 init segment.
        if not native_video_running:
            self._interrupt_relay()
        self.wake.set()
        return subscriber

    def unsubscribe_native_video(self, subscriber: queue.Queue[bytes]) -> None:
        with self.lock:
            self.native_video_subscribers.discard(subscriber)

    def start_native_player(self) -> dict:
        player_path = native_player_path()
        if player_path is None:
            raise RuntimeError("native FFmpeg player is not installed in this build")
        with self.native_lock:
            if self.native_process is not None and self.native_process.poll() is None:
                self.native_requested = True
                return self.native_player_status()
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                native_player_command(
                    player_path,
                    str(self.device.get("alias") or self.device.get("deviceName") or "Topo camera"),
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=creationflags,
            )
            self.native_process = process
            self.native_requested = True
            threading.Thread(
                target=self._read_native_output,
                args=(process,),
                name="tapo-native-player-log",
                daemon=True,
            ).start()
        # A native MPEG-TS decoder must see the beginning of a fresh stream
        # (PAT/PMT). If the browser relay is already running, interrupt it so
        # the worker reconnects and sends the complete stream to both outputs.
        self._interrupt_relay()
        self.wake.set()
        return self.native_player_status()

    def _interrupt_relay(self) -> None:
        with self.relay_lock:
            relay_socket = self.relay_socket
        if relay_socket is None:
            return
        with contextlib.suppress(OSError):
            relay_socket.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            relay_socket.close()

    @staticmethod
    def _read_native_output(process: subprocess.Popen) -> None:
        """Drain native-player diagnostics so a full pipe cannot stall it."""
        if process.stdout is None:
            return
        try:
            for raw_line in iter(process.stdout.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    print(f"[native-player] {line}", flush=True)
        except (OSError, ValueError):
            pass

    def stop_native_player(self) -> dict:
        with self.native_lock:
            process = self.native_process
            self.native_process = None
            self.native_requested = False
        self._terminate_native_process(process)
        self.wake.set()
        return {"active": False, "available": native_player_path() is not None}

    def native_player_status(self) -> dict:
        with self.native_lock:
            process = self.native_process
            if process is not None and process.poll() is not None:
                self.native_process = None
                self.native_requested = False
                process = None
            active = bool(self.native_requested and process is not None)
        return {"active": active, "available": native_player_path() is not None}

    @staticmethod
    def _terminate_native_process(process: subprocess.Popen | None) -> None:
        if process is None or process.poll() is not None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        with self.native_lock:
            native_process = self.native_process
            self.native_process = None
            self.native_requested = False
        self._terminate_native_process(native_process)
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

    def _broadcast_audio(self, chunk: bytes) -> None:
        with self.lock:
            subscribers = tuple(self.audio_subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(chunk)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(chunk)
                except queue.Full:
                    pass

    def _broadcast_native_video(self, chunk: bytes) -> None:
        with self.lock:
            subscribers = tuple(self.native_video_subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(chunk)
            except queue.Full:
                # Keep the newest MP4 fragment; an old fragment is not useful
                # for a live player that is already behind real time.
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(chunk)
                except queue.Full:
                    pass

    def _run(self) -> None:
        while not self.stop.is_set():
            with self.lock:
                has_subscribers = bool(
                    self.subscribers or self.audio_subscribers or self.native_video_subscribers
                )
            with self.native_lock:
                has_native_player = bool(self.native_requested and self.native_process)
            if not has_subscribers and not has_native_player:
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
            "resolution": STREAM_RESOLUTION,
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
        audio_ffmpeg = None
        audio_thread = None
        native_video_ffmpeg = None
        native_video_thread = None
        audio_extractor = _MulawTsExtractor()
        with self.lock:
            needs_video_decoder = bool(self.subscribers)
            needs_audio_decoder = bool(self.audio_subscribers)
            needs_native_video = bool(self.native_video_subscribers)
        native_process = None
        try:
            with socket.create_connection((host, parsed.port or 443), timeout=15) as plain_socket:
                with self.client.verify_context.wrap_socket(
                    plain_socket, server_hostname=host
                ) as tls_socket:
                    with self.relay_lock:
                        self.relay_socket = tls_socket
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
                    if needs_video_decoder or needs_audio_decoder:
                        ffmpeg = subprocess.Popen(
                            [
                            self.client_ffmpeg,
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            # Keep the live MPEG-TS path close to the newest
                            # packet. The mobile app uses a native low-latency
                            # player; without these flags FFmpeg may retain a
                            # small input buffer before emitting frames.
                            "-fflags",
                            "nobuffer",
                            "-flags",
                            "low_delay",
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
                        if needs_video_decoder:
                            jpeg_thread = threading.Thread(
                                target=self._read_jpegs,
                                args=(ffmpeg,),
                                name="tapo-jpeg-reader",
                                daemon=True,
                            )
                            jpeg_thread.start()
                        if needs_audio_decoder:
                            audio_ffmpeg, audio_thread = self._start_audio_encoder()
                    if needs_native_video:
                        with self.lock:
                            self.native_video_running = True
                            self.native_init_segment = b""
                        native_video_ffmpeg = subprocess.Popen(
                            [
                                self.client_ffmpeg,
                                "-hide_banner",
                                "-loglevel",
                                "error",
                                "-fflags",
                                "nobuffer",
                                "-flags",
                                "low_delay",
                                "-analyzeduration",
                                "0",
                                "-probesize",
                                "32768",
                                "-f",
                                "mpegts",
                                "-i",
                                "pipe:0",
                                "-map",
                                "0:v:0",
                                "-c:v",
                                "copy",
                                "-an",
                                "-flush_packets",
                                "1",
                                "-muxdelay",
                                "0",
                                "-muxpreload",
                                "0",
                                "-movflags",
                                # Flush every encoded frame instead of waiting
                                # for the camera's next keyframe. The C200 GOP
                                # is roughly two seconds; keyframe-only MP4
                                # fragments therefore make a browser player
                                # sit about two seconds behind the live edge.
                                "+empty_moov+default_base_moof+frag_every_frame",
                                "-f",
                                "mp4",
                                "pipe:1",
                            ],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            bufsize=0,
                        )
                        native_video_thread = threading.Thread(
                            target=self._read_native_video,
                            args=(native_video_ffmpeg,),
                            name="tapo-native-video-reader",
                            daemon=True,
                        )
                        native_video_thread.start()
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
                        # The native player can be opened after this relay has
                        # already started for the browser preview. Resolve it
                        # for every part so the new player joins the existing
                        # live session instead of waiting for a reconnect.
                        with self.native_lock:
                            current_native_process = (
                                self.native_process
                                if self.native_requested
                                else None
                            )
                        if current_native_process is not None and current_native_process.poll() is not None:
                            self.native_player_status()
                            current_native_process = None
                        native_process = current_native_process
                        outputs = []
                        if is_video and ffmpeg is not None and ffmpeg.stdin is not None:
                            outputs.append(ffmpeg.stdin)
                        if (
                            is_video
                            and native_video_ffmpeg is not None
                            and native_video_ffmpeg.stdin is not None
                        ):
                            outputs.append(native_video_ffmpeg.stdin)
                        if is_video and current_native_process is not None and current_native_process.stdin is not None:
                            outputs.append(native_process.stdin)
                        if is_video and outputs:
                            self._pipe_exact(
                                tls_socket,
                                buffer,
                                length,
                                outputs,
                                audio_extractor,
                                audio_ffmpeg,
                            )
                        else:
                            self._read_exact(tls_socket, buffer, length)
                    with self.relay_lock:
                        if self.relay_socket is tls_socket:
                            self.relay_socket = None
        finally:
            with self.relay_lock:
                if self.relay_socket is not None and self.relay_socket.fileno() < 0:
                    self.relay_socket = None
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
            if native_video_ffmpeg is not None and native_video_ffmpeg.stdin is not None:
                try:
                    native_video_ffmpeg.stdin.close()
                except OSError:
                    pass
            if native_video_ffmpeg is not None:
                try:
                    native_video_ffmpeg.terminate()
                    native_video_ffmpeg.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    native_video_ffmpeg.kill()
                if native_video_thread is not None:
                    native_video_thread.join(timeout=2)
            if native_video_ffmpeg is not None:
                with self.lock:
                    self.native_video_running = False
            if native_process is not None and native_process.poll() is not None:
                self.native_player_status()
            if audio_ffmpeg is not None and audio_ffmpeg.stdin is not None:
                try:
                    audio_ffmpeg.stdin.close()
                except OSError:
                    pass
            if audio_ffmpeg is not None:
                try:
                    audio_ffmpeg.terminate()
                    audio_ffmpeg.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    audio_ffmpeg.kill()
                if audio_thread is not None:
                    audio_thread.join(timeout=2)

    def _has_audio_subscribers(self) -> bool:
        with self.lock:
            return bool(self.audio_subscribers)

    def _start_audio_encoder(self) -> tuple[subprocess.Popen, threading.Thread]:
        encoder = subprocess.Popen(
            [
                self.client_ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "mulaw",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-write_xing",
                "0",
                "-flush_packets",
                "1",
                "-f",
                "mp3",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        reader = threading.Thread(
            target=self._read_audio,
            args=(encoder,),
            name="tapo-audio-reader",
            daemon=True,
        )
        reader.start()
        return encoder, reader

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
        outputs: list,
        audio_extractor: _MulawTsExtractor | None = None,
        audio_ffmpeg: subprocess.Popen | None = None,
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
            chunk = bytes(buffer[:amount])
            for output in outputs:
                try:
                    output.write(chunk)
                    output.flush()
                except (BrokenPipeError, OSError) as exc:
                    raise BrokenPipeError("native or browser media player closed") from exc
            if audio_extractor is not None and audio_ffmpeg is not None:
                audio = audio_extractor.feed(bytes(buffer[:amount]))
                if audio and audio_ffmpeg.stdin is not None and audio_ffmpeg.poll() is None:
                    try:
                        audio_ffmpeg.stdin.write(audio)
                        audio_ffmpeg.stdin.flush()
                    except OSError:
                        pass
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

    def _read_native_video(self, ffmpeg: subprocess.Popen) -> None:
        if ffmpeg.stdout is None:
            return
        read1 = getattr(ffmpeg.stdout, "read1", ffmpeg.stdout.read)
        init_buffer = bytearray()
        init_segment_saved = False
        while True:
            chunk = read1(16384)
            if not chunk:
                return
            if not init_segment_saved:
                init_buffer.extend(chunk)
                # The fragmented MP4 stream starts with ftyp + moov and then
                # the first moof. Cache the bytes before that first moof so a
                # browser subscribing after remuxer startup can still receive
                # a complete Media Source initialization segment.
                moof = init_buffer.find(b"moof")
                if moof >= 4:
                    init_segment = bytes(init_buffer[: moof - 4])
                    with self.lock:
                        self.native_init_segment = init_segment
                    init_segment_saved = True
            self._broadcast_native_video(chunk)

    def _read_audio(self, ffmpeg: subprocess.Popen) -> None:
        if ffmpeg.stdout is None:
            return
        read1 = getattr(ffmpeg.stdout, "read1", ffmpeg.stdout.read)
        while True:
            chunk = read1(8192)
            if not chunk:
                return
            self._broadcast_audio(chunk)

    @property
    def client_ffmpeg(self) -> str:
        return os.environ.get("TOPO_FFMPEG") or getattr(self.client, "ffmpeg", "ffmpeg")


class CloudAppState:
    STATUS_CACHE_TTL = 10.0
    RECORDING_PREVIEW_SECONDS = 10

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
        self.recording_jobs: dict[str, dict] = {}
        self.recording_active: dict[int, str] = {}
        self.recording_jobs_lock = threading.Lock()
        self.recording_dir = Path(tempfile.mkdtemp(prefix="topo-recordings-"))

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
        shutil.rmtree(self.recording_dir, ignore_errors=True)
        self.client.close()

    def reset_worker(self, index: int) -> None:
        """Drop a relay that may have become stale after a camera state change."""
        with self.workers_lock:
            worker = self.workers.pop(index, None)
        if worker is not None:
            worker.close()

    def native_player(self, index: int, enabled: bool) -> dict:
        if enabled:
            return self.worker(index).start_native_player()
        with self.workers_lock:
            worker = self.workers.get(index)
        if worker is None:
            return {"active": False, "available": native_player_path() is not None}
        return worker.stop_native_player()

    def native_player_status(self, index: int) -> dict:
        with self.workers_lock:
            worker = self.workers.get(index)
        if worker is None:
            return {"active": False, "available": native_player_path() is not None}
        return worker.native_player_status()

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

    def recording_dates(self, index: int, days: int = 7) -> dict:
        return self.client.recording_dates(self.base_url, self.devices[index], days)

    def recordings_for_day(self, index: int, date: str) -> dict:
        return self.client.recordings_for_day(self.base_url, self.devices[index], date)

    @staticmethod
    def _extract_mulaw_audio(ts_path: Path, audio_path: Path) -> int:
        """Extract Tapo's private G.711 μ-law PES audio from an MPEG-TS file."""
        data = ts_path.read_bytes()
        audio_pids: set[int] = set()
        for offset in range(0, len(data) - 187, 188):
            packet = data[offset : offset + 188]
            if packet[0] != 0x47 or not (packet[1] & 0x40):
                continue
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            adaptation = (packet[3] >> 4) & 0x3
            if not adaptation & 1:
                continue
            payload_offset = 4
            if adaptation & 2:
                payload_offset += 1 + packet[4]
            payload = packet[payload_offset:]
            if len(payload) >= 4 and payload[:3] == b"\x00\x00\x01":
                stream_id = payload[3]
                if 0xC0 <= stream_id <= 0xDF:
                    audio_pids.add(pid)
        if not audio_pids:
            return 0

        audio = bytearray()
        remaining_by_pid: dict[int, int] = {}
        for offset in range(0, len(data) - 187, 188):
            packet = data[offset : offset + 188]
            if packet[0] != 0x47:
                continue
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            if pid not in audio_pids:
                continue
            adaptation = (packet[3] >> 4) & 0x3
            if not adaptation & 1:
                continue
            payload_offset = 4
            if adaptation & 2:
                payload_offset += 1 + packet[4]
            if payload_offset >= 188:
                continue
            payload = packet[payload_offset:]
            if packet[1] & 0x40:
                remaining_by_pid[pid] = 0
                if len(payload) < 14 or payload[:3] != b"\x00\x00\x01":
                    continue
                packet_length = int.from_bytes(payload[4:6], "big")
                header_length = 9 + payload[8]
                if packet_length <= 0 or header_length > 6 + packet_length:
                    continue
                remaining_by_pid[pid] = 6 + packet_length - header_length
                payload = payload[header_length:]
            remaining = remaining_by_pid.get(pid, 0)
            if remaining <= 0:
                continue
            take = min(len(payload), remaining)
            audio.extend(payload[:take])
            remaining_by_pid[pid] = remaining - take
        if not audio:
            return 0
        audio_path.write_bytes(audio)
        return len(audio)

    def start_recording(self, index: int, start: int, end: int) -> str:
        if end <= start:
            raise ValueError("recording end must be after start")
        if end - start > 3600:
            raise ValueError("recording segments longer than one hour are not supported")
        job_id = uuid.uuid4().hex
        job = {
            "status": "preparing",
            "camera": index,
            "start": start,
            "end": end,
            "progress": 0,
            "message": "Starting recording preview…",
            "error": "",
            "path": str(self.recording_dir / f"{job_id}.mp4"),
        }
        with self.recording_jobs_lock:
            active_job_id = self.recording_active.get(index)
            if active_job_id:
                active_job = self.recording_jobs.get(active_job_id)
                if active_job and active_job.get("status") == "preparing":
                    raise RuntimeError(
                        "another recording preview is already preparing for this camera"
                    )
                self.recording_active.pop(index, None)
            self.recording_jobs[job_id] = job
            self.recording_active[index] = job_id
        thread = threading.Thread(
            target=self._prepare_recording,
            args=(job_id, index, start, end),
            name=f"topo-recording-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return job_id

    def _prepare_recording(self, job_id: str, index: int, start: int, end: int) -> None:
        ts_path = self.recording_dir / f"{job_id}.ts"
        audio_path = self.recording_dir / f"{job_id}.mulaw"
        mp4_path = self.recording_dir / f"{job_id}.mp4"

        def update_job(**values: object) -> None:
            with self.recording_jobs_lock:
                job = self.recording_jobs.get(job_id)
                if job is not None:
                    job.update(values)

        def report_progress(progress: int, message: str) -> None:
            update_job(progress=max(0, min(progress, 99)), message=message)

        try:
            bytes_written = self.client.download_recording(
                self.base_url,
                self.devices[index],
                start,
                end,
                str(ts_path),
                progress=report_progress,
            )
            if bytes_written <= 0:
                raise RuntimeError("recording relay returned no media")
            audio_bytes = self._extract_mulaw_audio(ts_path, audio_path)
            update_job(progress=85, message="Converting recording…")
            ffmpeg = os.environ.get("TOPO_FFMPEG") or "ffmpeg"
            ffmpeg_args = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(ts_path),
            ]
            if audio_bytes:
                ffmpeg_args.extend(
                    [
                        "-f",
                        "mulaw",
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        "-i",
                        str(audio_path),
                    ]
                )
            ffmpeg_args.extend(
                [
                    "-t",
                    str(max(end - start, 1)),
                    "-map",
                    "0:v:0",
                ]
            )
            if audio_bytes:
                ffmpeg_args.extend(
                    ["-map", "1:a:0", "-c:a", "aac", "-b:a", "64k"]
                )
            else:
                ffmpeg_args.append("-an")
            ffmpeg_args.extend(
                [
                    "-c:v",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(mp4_path),
                ]
            )
            result = subprocess.run(
                ffmpeg_args,
                capture_output=True,
                text=True,
                timeout=max((end - start) * 5 + 180, 300),
                check=False,
            )
            if result.returncode != 0 or not mp4_path.is_file() or mp4_path.stat().st_size == 0:
                detail = (result.stderr or "").strip().splitlines()[-1:] or ["FFmpeg could not prepare the recording"]
                raise RuntimeError(detail[0][:240])
            with self.recording_jobs_lock:
                job = self.recording_jobs.get(job_id)
                if job is not None:
                    job.update(
                        {
                            "status": "ready",
                            "progress": 100,
                            "message": "Recording ready.",
                            "size": mp4_path.stat().st_size,
                        }
                    )
        except Exception as exc:
            with self.recording_jobs_lock:
                job = self.recording_jobs.get(job_id)
                if job is not None:
                    job.update(
                        {
                            "status": "error",
                            "message": "Recording preparation failed.",
                            "error": str(exc)[:240],
                        }
                    )
            with contextlib.suppress(OSError):
                mp4_path.unlink()
        finally:
            with contextlib.suppress(OSError):
                ts_path.unlink()
            with contextlib.suppress(OSError):
                audio_path.unlink()
            with self.recording_jobs_lock:
                if self.recording_active.get(index) == job_id:
                    self.recording_active.pop(index, None)

    def recording_job(self, job_id: str) -> dict | None:
        with self.recording_jobs_lock:
            job = self.recording_jobs.get(job_id)
            if job is None:
                return None
            result = {key: value for key, value in job.items() if key != "path"}
        if result.get("status") == "ready":
            result["video_url"] = f"/recordings/{job_id}.mp4"
        return result

    def recording_path(self, job_id: str) -> Path | None:
        with self.recording_jobs_lock:
            job = self.recording_jobs.get(job_id)
            if not job or job.get("status") != "ready":
                return None
            path = Path(str(job.get("path") or ""))
        return path if path.is_file() and path.parent == self.recording_dir else None

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
            "native_available": ffmpeg_command_available(),
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
            reset_terminal_uuid()
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
        # A logout must also forget this installation's Tapo terminal identity.
        # Otherwise Tapo may consider the next login trusted and skip MFA,
        # which makes Linux and Windows behave differently after logout.
        reset_terminal_uuid()

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
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            self.wfile.write(body)

    def send_file(self, path: Path, download_name: str | None = None) -> None:
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
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
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
            # Do not expose an HTTP 200 multipart stream until the camera has
            # produced a real JPEG. An offline camera can otherwise leave the
            # browser attached to an endless, empty MJPEG response. WebKitGTK
            # handles that state poorly and may terminate the whole web
            # process instead of reporting an image error.
            deadline = time.monotonic() + STREAM_START_TIMEOUT
            first_frame = None
            while first_frame is None:
                if self.context.current_state() is not state:
                    raise ConnectionError("session ended")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("camera did not provide a video frame")
                try:
                    first_frame = subscriber.get(timeout=min(1.0, remaining))
                except queue.Empty:
                    if worker.last_error:
                        raise RuntimeError(worker.last_error)

            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self._write_mjpeg_frame(first_frame)
            while True:
                if self.context.current_state() is not state:
                    raise ConnectionError("session ended")
                try:
                    frame = subscriber.get(timeout=20)
                except queue.Empty:
                    if worker.last_error:
                        raise ConnectionError(worker.last_error)
                    continue
                self._write_mjpeg_frame(frame)
        except (TimeoutError, RuntimeError) as exc:
            self._send_stream_unavailable(str(exc))
        except (BrokenPipeError, ConnectionResetError, ConnectionError, OSError):
            pass
        finally:
            worker.unsubscribe(subscriber)

    def stream_native_video(self, state: CloudAppState, index: int) -> None:
        worker = state.worker(index)
        subscriber = worker.subscribe_native_video()
        try:
            deadline = time.monotonic() + max(STREAM_START_TIMEOUT, 30.0)
            first_chunk = None
            while first_chunk is None:
                if self.context.current_state() is not state:
                    raise ConnectionError("session ended")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("camera did not provide a low-latency video stream")
                try:
                    first_chunk = subscriber.get(timeout=min(1.0, remaining))
                except queue.Empty:
                    # Opening low-latency mode intentionally interrupts an
                    # existing browser relay. Its first EOF is transient;
                    # wait for the worker to establish the fresh relay.
                    continue

            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(first_chunk)
            self.wfile.flush()
            while True:
                if self.context.current_state() is not state:
                    raise ConnectionError("session ended")
                try:
                    chunk = subscriber.get(timeout=20)
                except queue.Empty:
                    if worker.last_error:
                        raise ConnectionError(worker.last_error)
                    continue
                self.wfile.write(chunk)
                self.wfile.flush()
        except (TimeoutError, RuntimeError) as exc:
            self._send_stream_unavailable(str(exc))
        except (BrokenPipeError, ConnectionResetError, ConnectionError, OSError):
            pass
        finally:
            worker.unsubscribe_native_video(subscriber)

    def _write_mjpeg_frame(self, frame: bytes) -> None:
        self.wfile.write(
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(frame)).encode("ascii")
            + b"\r\n\r\n"
            + frame
            + b"\r\n"
        )
        self.wfile.flush()

    def _send_stream_unavailable(self, message: str) -> None:
        body = (message or "camera stream unavailable").encode("utf-8", "replace")
        try:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def stream_audio(self, state: CloudAppState, index: int) -> None:
        worker = state.worker(index)
        subscriber = worker.subscribe_audio()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                if self.context.current_state() is not state:
                    raise ConnectionError("session ended")
                try:
                    chunk = subscriber.get(timeout=20)
                except queue.Empty:
                    if worker.last_error:
                        raise ConnectionError(worker.last_error)
                    continue
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionError, OSError):
            pass
        finally:
            worker.unsubscribe_audio(subscriber)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed_url = urlsplit(self.path)
        request_path = parsed_url.path
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
        if request_path.startswith("/api/recording-jobs/"):
            state = self.require_state()
            if state is None:
                return
            job_id = request_path[len("/api/recording-jobs/") :].strip("/")
            if not job_id or "/" in job_id:
                self.send_json({"ok": False, "error": "invalid recording job"}, 404)
                return
            job = state.recording_job(job_id)
            if job is None:
                self.send_json({"ok": False, "error": "recording job not found"}, 404)
            else:
                self.send_json({"ok": True, "job_id": job_id, **job})
            return
        if request_path.startswith("/recordings/") and request_path.endswith(".mp4"):
            state = self.require_state()
            if state is None:
                return
            job_id = request_path[len("/recordings/") : -len(".mp4")].strip("/")
            if not job_id or "/" in job_id:
                self.send_error(404)
                return
            path = state.recording_path(job_id)
            if path is None:
                self.send_error(404)
            else:
                download_name = None
                if parse_qs(parsed_url.query).get("download") == ["1"]:
                    download_name = f"topo-recording-{job_id[:8]}.mp4"
                self.send_file(path, download_name=download_name)
            return
        if request_path == "/api/cameras":
            state = self.require_state()
            if state is not None:
                self.send_json({"cameras": state.public_cameras()})
            return
        if request_path.startswith("/api/cameras/"):
            recordings_prefix = "/api/cameras/"
            recordings_parts = request_path[len(recordings_prefix) :].strip("/").split("/")
            if len(recordings_parts) == 2 and recordings_parts[1] == "native-player":
                state = self.require_state()
                if state is None:
                    return
                try:
                    index = int(recordings_parts[0])
                    state.devices[index]
                    self.send_json({"ok": True, **state.native_player_status(index)})
                except (ValueError, IndexError) as exc:
                    self.send_json({"ok": False, "error": str(exc)}, 400)
                return
            if len(recordings_parts) == 3 and recordings_parts[1] == "recordings" and recordings_parts[2] == "dates":
                state = self.require_state()
                if state is None:
                    return
                try:
                    index = int(recordings_parts[0])
                    state.devices[index]
                    raw_days = parse_qs(parsed_url.query).get("days", ["7"])[0]
                    days = int(raw_days)
                    if not 1 <= days <= 31:
                        raise ValueError("days must be between 1 and 31")
                    self.send_json({"ok": True, **state.recording_dates(index, days)})
                except (ValueError, IndexError) as exc:
                    self.send_json({"ok": False, "error": str(exc)}, 400)
                except (RuntimeError, OSError) as exc:
                    self.send_json({"ok": False, "error": str(exc)}, 502)
                return
            if len(recordings_parts) == 2 and recordings_parts[1] == "recordings":
                state = self.require_state()
                if state is None:
                    return
                try:
                    index = int(recordings_parts[0])
                    state.devices[index]
                    date = parse_qs(parsed_url.query).get("date", [""])[0]
                    if not date:
                        raise ValueError("date is required")
                    self.send_json({"ok": True, **state.recordings_for_day(index, date)})
                except (ValueError, IndexError) as exc:
                    self.send_json({"ok": False, "error": str(exc)}, 400)
                except (RuntimeError, OSError) as exc:
                    self.send_json({"ok": False, "error": str(exc)}, 502)
                return
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
        if request_path.startswith("/stream/") and request_path.endswith(".mp4"):
            state = self.require_state()
            if state is None:
                return
            raw_index = request_path[len("/stream/") : -len(".mp4")].strip("/")
            try:
                index = int(raw_index)
                state.devices[index]
            except (ValueError, IndexError):
                self.send_json({"ok": False, "error": "invalid camera"}, 404)
                return
            self.stream_native_video(state, index)
            return
        if request_path.startswith("/audio/") and request_path.endswith(".mp3"):
            state = self.require_state()
            if state is None:
                return
            raw_index = request_path[len("/audio/") : -len(".mp3")].strip("/")
            try:
                index = int(raw_index)
                state.devices[index]
            except (ValueError, IndexError):
                self.send_json({"ok": False, "error": "invalid camera"}, 404)
                return
            self.stream_audio(state, index)
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
        if request_path.startswith(prefix) and request_path.endswith("/recordings/play"):
            raw_index = request_path[len(prefix) : -len("/recordings/play")].strip("/")
            try:
                index = int(raw_index)
                state.devices[index]
                payload = self.read_json_body()
                start = payload.get("start")
                end = payload.get("end")
                if (
                    isinstance(start, bool)
                    or isinstance(end, bool)
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                ):
                    raise ValueError("start and end must be integers")
                job_id = state.start_recording(index, start, end)
                self.send_json({"ok": True, "job_id": job_id}, 202)
            except (ValueError, IndexError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            except (RuntimeError, OSError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 502)
            return
        if request_path.endswith("/native-player"):
            suffix = "/native-player"
            action = "native-player"
        elif request_path.endswith("/privacy"):
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
            elif action == "native-player":
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
            elif action == "native-player":
                self.send_json({"ok": True, **state.native_player(index, enabled)})
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
    actual_port = server.server_address[1]
    print(f"{PRODUCT_NAME} available at http://{args.host}:{actual_port}", flush=True)
    print("Press Ctrl+C to stop the app.", flush=True)
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
