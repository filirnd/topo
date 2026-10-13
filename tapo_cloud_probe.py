#!/usr/bin/env python3
"""Minimal Tapo cloud-only probe.

This is an exploratory client for the private Tapo app API. It logs in using
credentials typed into the terminal and lists the cameras visible to the
account. Only the cloud session is saved locally; the account password is
never written to disk.

The API is private and may change without notice.  The app-level signing keys
are public constants extracted from the official Android client; they are not
the user's account credentials.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import http.client
import json
import os
import platform
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.parse import parse_qsl, urlunsplit


APP_NAME = "TP-Link_Tapo_Android"
APP_VERSION = "3.21.114"
PRODUCT_NAME = "Topo - desktop app for Tapo camera"
LOGIN_URL = "https://n-wap-gw.tplinkcloud.com"
CIPC_URL = "https://cipc.api.cloud"

# App-level credentials used by the Tapo Android application.  These identify
# the client application, not the user's Tapo account.
ACCESS_KEY = "4d11b6b9d5ea4d19a829adbb9714b057"
ACCESS_SECRET = "6ed7d97f3e73467f8a5bab90b577ba4c"

DEVICE_ERROR_MESSAGES = {
    -64303: "motor busy; wait for the movement or patrol to finish",
    -64304: "maximum pan/tilt limit reached",
    -64324: "privacy mode enabled",
    -64336: "movement blocked by a privacy zone",
}


def session_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return root / "tapo-cloud-probe" / "session.json"


def load_session() -> dict | None:
    path = session_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("token"):
        return None
    return data


def save_session(base_url: str, result: dict) -> Path:
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    data = {
        "base_url": base_url,
        "token": result.get("token"),
        "refreshToken": result.get("refreshToken"),
        "saved_at": int(time.time()),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="session.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return path


def clear_session() -> None:
    """Remove the locally cached cloud session, if present."""
    try:
        session_path().unlink()
    except FileNotFoundError:
        pass


def error_code(value: object) -> int:
    if not isinstance(value, dict):
        return 0
    for key in ("error_code", "errorCode"):
        if key in value:
            try:
                return int(value[key])
            except (TypeError, ValueError):
                return 1
    return 0


def result_of(response: dict) -> dict:
    value = response.get("result")
    return value if isinstance(value, dict) else {}


def mask_id(value: object) -> str:
    text = str(value or "")
    if len(text) <= 10:
        return "<present>" if text else "<missing>"
    return text[:6] + "…" + text[-4:]


class TapoCloud:
    def __init__(self, insecure: bool = False):
        self.token = ""
        self.cipc_url = CIPC_URL
        self._cipc_discovery_base_url = ""
        self.iot_app_server = ""
        # The official app keeps an HTTP connection pool for camera control.
        # Reusing the TLS connection avoids a handshake on every PTZ/privacy
        # request, which is especially noticeable when the client is remote.
        self._http_connections: dict[
            tuple[str, int, bool],
            tuple[http.client.HTTPSConnection, threading.Lock],
        ] = {}
        self._http_connections_lock = threading.Lock()
        # Tapo's current Android client uses the v2 services-sync endpoint.
        # Keep a compatibility fallback for older regional gateways.
        self._services_sync_version = "v2"
        self.last_cipc_player_id = ""
        self.terminal_uuid = uuid.uuid4().hex.upper()
        self.verify_context = ssl.create_default_context()
        if insecure:
            self.verify_context.check_hostname = False
            self.verify_context.verify_mode = ssl.CERT_NONE

    def close(self) -> None:
        """Close pooled cloud connections when the desktop app exits."""
        with self._http_connections_lock:
            connections = list(self._http_connections.values())
            self._http_connections.clear()
        for connection, lock in connections:
            with lock:
                connection.close()

    def _persistent_post(
        self, url: str, body: bytes, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        """POST over a reusable HTTPS connection.

        The request is deliberately not retried automatically: a PTZ POST
        may have reached the camera even if the response connection drops, so
        retrying could execute the physical movement twice.
        """
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError("cloud endpoint is not HTTPS or has no host")
        port = parsed.port or 443
        key = (parsed.hostname, port, self.verify_context.check_hostname)
        with self._http_connections_lock:
            entry = self._http_connections.get(key)
            if entry is None:
                connection = http.client.HTTPSConnection(
                    parsed.hostname,
                    port=port,
                    context=self.verify_context,
                    timeout=30,
                )
                entry = (connection, threading.Lock())
                self._http_connections[key] = entry
        connection, lock = entry
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        with lock:
            try:
                connection.request("POST", path, body=body, headers=headers)
                response = connection.getresponse()
                raw = response.read()
                should_close = response.will_close
            except (http.client.HTTPException, OSError):
                with self._http_connections_lock:
                    if self._http_connections.get(key) is entry:
                        self._http_connections.pop(key, None)
                connection.close()
                raise
            if should_close:
                with self._http_connections_lock:
                    if self._http_connections.get(key) is entry:
                        self._http_connections.pop(key, None)
                connection.close()
            return response.status, raw

    @staticmethod
    def _body(payload: dict) -> bytes:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()

    @staticmethod
    def _content_md5(body: bytes) -> str:
        return base64.b64encode(hashlib.md5(body).digest()).decode("ascii")

    def _headers(self, body: bytes, endpoint: str) -> dict[str, str]:
        nonce = str(uuid.uuid4())
        timestamp = "9999999999"
        content_md5 = self._content_md5(body)
        message = "\n".join((content_md5, timestamp, nonce, endpoint))
        signature = hmac.new(
            ACCESS_SECRET.encode(), message.encode(), hashlib.sha1
        ).hexdigest()
        return {
            "Content-Type": "application/json; charset=UTF-8",
            "Content-MD5": content_md5,
            "X-Authorization": (
                f"Timestamp={timestamp}, Nonce={nonce}, "
                f"AccessKey={ACCESS_KEY}, Signature={signature}"
            ),
            "User-Agent": "okhttp/3.12.13",
        }

    def post(self, base_url: str, endpoint: str, payload: dict, *, token: bool) -> dict:
        body = self._body(payload)
        query = {
            "token": self.token,
        } if token else {}
        url = base_url.rstrip("/") + endpoint
        if query:
            url += "?" + urlencode(query)
        request = Request(
            url,
            data=body,
            headers=self._headers(body, endpoint),
            method="POST",
        )
        try:
            with urlopen(request, context=self.verify_context, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            print(f"HTTP {exc.code} for {endpoint}")
        except URLError as exc:
            raise RuntimeError(f"network error for {endpoint}: {exc.reason}") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"non-JSON response from {endpoint}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"unexpected response from {endpoint}")
        return decoded

    def post_cipc(self, endpoint: str, payload: dict, player_id: str) -> dict:
        body = self._body(payload)
        url = self.cipc_url.rstrip("/") + endpoint
        request = Request(
            url,
            data=body,
            headers={
                # CIPC expects the raw cloud token. The ordinary account API
                # uses the separate `ut|` prefix, but the Android media
                # interceptor does not add it for this service.
                "Authorization": self.token,
                "Content-Type": "application/json; charset=UTF-8",
                "User-Agent": (
                    f"Tapo_APP/Linux/{APP_VERSION}_Android/Android "
                    f"{platform.release()}"
                ),
                "X-Client-Id": player_id,
                "X-Source": "tapo-app",
                "X-Brand": "TPLINK",
                "X-Ca-Type": "cloud-self",
                # The Android CIPC client attaches a short per-request
                # signature/trace id to relay URL requests. It is not the
                # account token; it only identifies this media transaction.
                "X-Request-Signature": uuid.uuid4().hex[:10],
            },
            method="POST",
        )
        try:
            with urlopen(request, context=self.verify_context, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            print(f"HTTP {exc.code} for CIPC {endpoint}")
        except URLError as exc:
            self._cipc_discovery_base_url = ""
            raise RuntimeError(f"CIPC network error: {exc.reason}") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("CIPC returned a non-JSON response") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("unexpected CIPC response")
        return decoded

    def discover_cipc_url(self, base_url: str, *, force: bool = False) -> str:
        if not force and self._cipc_discovery_base_url == base_url:
            return self.cipc_url
        response = self.post(
            base_url,
            "/api/v2/common/getAppServiceUrl",
            {"serviceIds": ["cipc.api.cloud"]},
            token=True,
        )
        if error_code(response) != 0:
            raise RuntimeError(
                f"CIPC endpoint discovery rejected: code {error_code(response)}"
            )
        services = result_of(response).get("serviceUrls")
        if not isinstance(services, dict):
            raise RuntimeError("CIPC endpoint response has no serviceUrls")
        service_url = services.get("cipc.api.cloud")
        if not service_url:
            raise RuntimeError("cipc.api.cloud endpoint missing from response")
        self.cipc_url = str(service_url).replace("http://", "https://", 1)
        self._cipc_discovery_base_url = base_url
        return self.cipc_url

    def discover_iot_app_server(self, base_url: str) -> str:
        """Discover the modern cloud control gateway used by Tapo cameras."""
        response = self.post(
            base_url,
            "/api/v2/common/getAppServiceUrl",
            {"serviceIds": ["nbu.iot-app-server.app"]},
            token=True,
        )
        if error_code(response) != 0:
            raise RuntimeError(
                f"cloud control endpoint discovery rejected: code {error_code(response)}"
            )
        services = result_of(response).get("serviceUrls")
        if not isinstance(services, dict):
            raise RuntimeError("cloud control response has no serviceUrls")
        service_url = services.get("nbu.iot-app-server.app")
        if not service_url:
            raise RuntimeError("cloud control endpoint missing from response")
        self.iot_app_server = str(service_url).replace("http://", "https://", 1)
        return self.iot_app_server

    def _iot_headers(self) -> dict[str, str]:
        return {
            "Authorization": "ut|" + self.token,
            "app-cid": f"app:{APP_NAME}:{self.terminal_uuid}",
            "x-app-name": APP_NAME,
            "x-app-version": APP_VERSION,
            "x-term-id": self.terminal_uuid,
            "x-ospf": f"{platform.system()} {platform.release()}",
            "x-net-type": "wifi",
            "x-strict": "0",
            "x-locale": "it_IT",
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}(cloud-only;Linux)",
        }

    def services_sync(
        self, base_url: str, device: dict, requests: list[dict]
    ) -> list[dict]:
        """Send camera control methods through Tapo's current cloud gateway."""
        device_id = str(device.get("deviceId") or "")
        if not device_id:
            raise RuntimeError("camera has no deviceId")
        if not self.iot_app_server:
            self.discover_iot_app_server(base_url)
        envelope = {
            "inputParams": {
                "requestData": {
                    "method": "multipleRequest",
                    "params": {"requests": requests},
                }
            },
            "serviceId": "passthrough",
        }
        body = self._body(envelope)
        url = (
            self.iot_app_server.rstrip("/")
            + f"/{self._services_sync_version}/things/"
            + quote(device_id, safe="")
            + "/services-sync"
        )
        try:
            status, raw = self._persistent_post(url, body, self._iot_headers())
        except (http.client.HTTPException, OSError) as exc:
            raise RuntimeError(
                f"cloud control network error: {exc}"
            ) from exc
        if status in (404, 405, 501) and self._services_sync_version == "v2":
            # Some older regional gateways still expose only v1. Remember the
            # result so subsequent PTZ clicks do not pay the failed request.
            self._services_sync_version = "v1"
            url = (
                self.iot_app_server.rstrip("/")
                + "/v1/things/"
                + quote(device_id, safe="")
                + "/services-sync"
            )
            try:
                status, raw = self._persistent_post(url, body, self._iot_headers())
            except (http.client.HTTPException, OSError) as exc:
                raise RuntimeError(
                    f"cloud control network error: {exc}"
                ) from exc
        if status >= 400:
            try:
                decoded = json.loads(raw.decode("utf-8"))
                message = decoded.get("message") or decoded.get("msg") or ""
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = ""
            detail = f": {message}" if message else ""
            raise RuntimeError(f"cloud control rejected (HTTP {status}){detail}")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("cloud control returned a non-JSON response") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("unexpected cloud control response")
        output = decoded.get("outputParams") or {}
        response_data = output.get("responseData") or {}
        result = response_data.get("result") or {}
        if error_code(response_data) != 0:
            raise RuntimeError(
                f"cloud control rejected: code {error_code(response_data)}"
            )
        responses = result.get("responses") or []
        if not isinstance(responses, list):
            raise RuntimeError("cloud control response has no responses")
        return responses

    @staticmethod
    def _privacy_response(responses: list[dict], method: str) -> dict:
        if not responses or not isinstance(responses[0], dict):
            raise RuntimeError(f"incomplete {method} response")
        response = responses[0]
        code = error_code(response)
        if code != 0:
            detail = DEVICE_ERROR_MESSAGES.get(code, "")
            suffix = f" ({detail})" if detail else ""
            raise RuntimeError(
                f"{method} rejected by camera: code {code}{suffix}"
            )
        return response

    def get_privacy_mode(self, base_url: str, device: dict) -> bool:
        responses = self.services_sync(
            base_url,
            device,
            [
                {
                    "method": "getLensMaskConfig",
                    "params": {"lens_mask": {"name": ["lens_mask_info"]}},
                }
            ],
        )
        response = self._privacy_response(responses, "getLensMaskConfig")
        result = response.get("result") or {}
        enabled = (
            result.get("lens_mask", {})
            .get("lens_mask_info", {})
            .get("enabled")
        )
        if enabled not in ("on", "off"):
            raise RuntimeError("privacy state is missing from the cloud response")
        return enabled == "on"

    def set_privacy_mode(
        self, base_url: str, device: dict, enabled: bool
    ) -> bool:
        responses = self.services_sync(
            base_url,
            device,
            [
                {
                    "method": "setLensMaskConfig",
                    "params": {
                        "lens_mask": {
                            "lens_mask_info": {"enabled": "on" if enabled else "off"}
                        }
                    },
                }
            ],
        )
        self._privacy_response(responses, "setLensMaskConfig")
        # Read back the state so the UI never reports success based only on an
        # acknowledgement from the cloud gateway.
        return self.get_privacy_mode(base_url, device)

    def ptz_supported(self, base_url: str, device: dict) -> bool:
        """Return whether the camera advertises the pan/tilt component."""
        responses = self.services_sync(
            base_url,
            device,
            [
                {
                    "method": "getAppComponentList",
                    "params": {"app_component": {"name": ["app_component_list"]}},
                }
            ],
        )
        response = self._privacy_response(responses, "getAppComponentList")
        result = response.get("result") or {}
        items = (
            result.get("app_component", {}).get("app_component_list", [])
            if isinstance(result.get("app_component"), dict)
            else []
        )
        return any(
            isinstance(item, dict) and item.get("name") == "ptz" for item in items
        )

    def move_motor(self, base_url: str, device: dict, x: int, y: int) -> None:
        """Move the camera by a relative pan/tilt step through the cloud."""
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
            raise ValueError("i valori pan/tilt devono essere interi")
        if not -200 <= x <= 200 or not -200 <= y <= 200:
            raise ValueError("movement must be between -200 and 200")
        responses = self.services_sync(
            base_url,
            device,
            [
                {
                    "method": "motorMove",
                    "params": {
                        "motor": {
                            "move": {"x_coord": str(x), "y_coord": str(y)}
                        }
                    },
                }
            ],
        )
        self._privacy_response(responses, "motorMove")

    def login(self, username: str, password: str) -> tuple[str, dict]:
        payload = {
            "appType": APP_NAME,
            "appVersion": APP_VERSION,
            "cloudPassword": password,
            "cloudUserName": username,
            "platform": f"Android {platform.release()}",
            "refreshTokenNeeded": False,
            "supportBindAccount": False,
            "terminalMeta": "1",
            "terminalName": f"{PRODUCT_NAME} Linux",
            "terminalUUID": self.terminal_uuid,
        }
        response = self.post(LOGIN_URL, "/api/v2/account/login", payload, token=False)
        if error_code(response) != 0:
            raise RuntimeError(
                f"login rejected: code {error_code(response)}"
            )
        result = result_of(response)
        # Accounts outside the global region return the correct regional URL.
        if error_code(result) == -20212 and result.get("appServerUrl"):
            regional_url = str(result["appServerUrl"])
            print(f"Account regionale rilevato: {regional_url}")
            response = self.post(
                regional_url, "/api/v2/account/login", payload, token=False
            )
            if error_code(response) != 0:
                raise RuntimeError(
                    f"regional login rejected: code {error_code(response)}"
                )
            result = result_of(response)
            return regional_url, result
        return LOGIN_URL, result

    def complete_mfa(
        self, base_url: str, username: str, password: str, result: dict
    ) -> dict:
        process_id = result.get("MFAProcessId")
        if not process_id:
            return result
        supported = result.get("supportedMFATypes") or [2]
        print(f"MFA is required. Available methods: {supported}")
        choice = input("MFA method (2=email, 1=app) [2]: ").strip() or "2"
        mfa_type = int(choice)
        self.request_mfa(base_url, username, password, mfa_type)
        if mfa_type == 1:
            print("Check the notification in the Tapo app.")
        else:
            print("Check the email associated with the Tapo account.")
        code = input("MFA code: ").strip()
        return self.verify_mfa(base_url, username, code, process_id, mfa_type)

    def request_mfa(
        self, base_url: str, username: str, password: str, mfa_type: int
    ) -> None:
        """Request an email code or app push for a pending login."""
        if mfa_type not in (1, 2):
            raise ValueError("MFA method is not supported")
        endpoint = (
            "/api/v2/account/getPushVC4TerminalMFA"
            if mfa_type == 1
            else "/api/v2/account/getEmailVC4TerminalMFA"
        )
        request_payload = {
            "appType": APP_NAME,
            "cloudPassword": password,
            "cloudUserName": username,
            "terminalUUID": self.terminal_uuid,
        }
        sent = self.post(base_url, endpoint, request_payload, token=False)
        if error_code(sent) != 0:
            raise RuntimeError(f"MFA code request failed: code {error_code(sent)}")

    def verify_mfa(
        self,
        base_url: str,
        username: str,
        code: str,
        process_id: str,
        mfa_type: int,
    ) -> dict:
        """Complete a pending MFA login with the user-provided code."""
        if not code:
            raise ValueError("MFA code is required")
        checked = self.post(
            base_url,
            "/api/v2/account/checkMFACodeAndLogin",
            {
                "appType": APP_NAME,
                "cloudUserName": username,
                "code": code,
                "MFAProcessId": process_id,
                "MFAType": mfa_type,
                "terminalBindEnabled": True,
            },
            token=False,
        )
        if error_code(checked) != 0:
            raise RuntimeError(f"MFA code rejected: code {error_code(checked)}")
        return result_of(checked)

    def list_cameras(self, base_url: str) -> list[dict]:
        payload = {
            "deviceTypeList": ["SMART.IPCAMERA"],
            "index": 0,
            "limit": 20,
        }
        response = self.post(
            base_url,
            "/api/v2/common/getDeviceListByPage",
            payload,
            token=True,
        )
        if error_code(response) != 0:
            raise RuntimeError(
                f"device list rejected: code {error_code(response)}"
            )
        result = result_of(response)
        devices = result.get("deviceList", [])
        return devices if isinstance(devices, list) else []

    def probe_legacy_relay(self, base_url: str, device: dict) -> None:
        """Try the older cloud relay negotiation used by C200-era clients.

        This is deliberately only a negotiation probe: it does not start a
        video session and it never prints the relay token returned by Tapo.
        Newer app versions may reject this compatibility endpoint; that result
        is useful evidence before implementing the current CIPC protocol.
        """
        device_id = str(device.get("deviceId") or "")
        if not device_id:
            raise RuntimeError("camera does not contain a deviceId")
        app_url = str(device.get("appServerUrl") or base_url)
        match = re.search(r"https://([a-z0-9]+)-", app_url)
        region = match.group(1) if match else "euw1"
        relay_server = f"{region}-relay-dcipc.i.tplinknbu.com"
        relay_request = {
            "token": self.token,
            "version": "1.3",
            "stream_type": 0,
            "protocol": 0,
            "relay_server": relay_server,
            "relay_port": 80,
            "relay_req_url": (
                f"/relayservice?deviceid={device_id}&type=video&resolution=HD"
            ),
            "local_req_url": "/stream",
        }
        payload = {
            "deviceId": device_id,
            "requestData": {
                "method": "do",
                "relay": {"request_relay": relay_request},
            },
        }
        response = self.post(
            app_url,
            "/api/v2/common/passthrough",
            payload,
            token=True,
        )
        outer_code = error_code(response)
        nested = result_of(response)
        nested_code = error_code(nested)
        print(
            "Relay probe: endpoint reached; "
            f"outer_code={outer_code}, inner_code={nested_code}"
        )
        if outer_code == 0 and nested_code == 0:
            print("Relay probe: negotiation accepted (token not displayed).")
        else:
            print("Relay probe: endpoint rejected or legacy protocol unsupported.")

    def probe_cipc_preconnection(
        self, base_url: str, device: dict, *, cloud_type: int = 2
    ) -> None:
        """Probe the current CIPC pre-connection endpoint without opening media."""
        device_id = str(device.get("deviceId") or "")
        if not device_id:
            raise RuntimeError("camera does not contain a deviceId")
        self.discover_cipc_url(base_url)
        player_id = str(uuid.uuid4())
        # The Android client reuses this UUID for the pre-connection and for
        # the following relay handshake.  Keep it so the stream probe can do
        # the same instead of creating a second client identity.
        self.last_cipc_player_id = player_id
        request_param = {
            "deviceId": device_id,
            # Current Tapo C200 firmware exposes the IoT-cloud relay. The
            # Android client selects 2 for that capability; 1 is the legacy
            # relay and is answered with "device offline" even when the
            # camera is reachable from the official app.
            "cloudType": cloud_type,
            "deviceType": device.get("deviceType") or "SMART.IPCAMERA",
            "playerId": player_id,
            "rootCaVer": "1",
            "streamType": 0,
            "preConnection": 1,
            "resolution": "HD",
        }
        payload = {
            "playerId": player_id,
            "requestList": [request_param],
        }
        response = self.post_cipc(
            "/v2/relay/preConnectionBatchRequest", payload, player_id
        )
        result = response
        if isinstance(response.get("result"), dict):
            result = response["result"]
        outer_code = error_code(result)
        response_list = result.get("preConnRespInfoList", [])
        first = response_list[0] if isinstance(response_list, list) and response_list else {}
        inner_code = error_code(first)
        relay_info = first.get("relayAccessInfo") if isinstance(first, dict) else None
        has_relay_info = isinstance(relay_info, dict) and bool(relay_info)
        print(
            "CIPC pre-connection: "
            f"outer_code={outer_code}, camera_code={inner_code}, "
            f"relay_parameters={'present' if has_relay_info else 'missing'}"
        )
        if outer_code == 0 and inner_code == 0 and has_relay_info:
            print("CIPC pre-connection accepted; token and URL not displayed.")
        else:
            print("CIPC pre-connection rejected.")
        return relay_info if has_relay_info else {}

    def probe_cipc_stream(
        self, base_url: str, device: dict, *, use_cdn: bool = False
    ) -> None:
        """Open the relay socket and inspect the first HTTP/multipart response."""
        # The pre-connection endpoint only warms the cloud relay and normally
        # returns an intentionally empty 200 response. The actual preview
        # uses the request endpoint with preConnection=0, then connects to the
        # returned relay URL.
        device_id = str(device.get("deviceId") or "")
        if not device_id:
            raise RuntimeError("camera does not contain a deviceId")
        self.discover_cipc_url(base_url)
        player_id = str(uuid.uuid4())
        self.last_cipc_player_id = player_id
        request_param = {
            "deviceId": device_id,
            "dataTimeoutCount": 0,
            "cloudType": 2,
            "deviceType": device.get("deviceType") or "SMART.IPCAMERA",
            "playerId": player_id,
            "rootCaVer": "1",
            "streamType": 0,
            "preConnection": 0,
            "resolution": "HD",
        }
        response = self.post_cipc("/v2/relay/request", request_param, player_id)
        relay_info = response.get("result") if isinstance(response.get("result"), dict) else {}
        if error_code(response) != 0 or not isinstance(relay_info, dict):
            raise RuntimeError(f"relay request rejected: code={error_code(response)}")
        relay_url = str(relay_info.get("relayUrl") or "")
        relay_token = str(relay_info.get("relayToken") or relay_info.get("sessionToken") or "")
        if not relay_url or not relay_token:
            raise RuntimeError("pre-connection response has no relayUrl or relayToken")
        if "://" not in relay_url:
            relay_url = "https://" + relay_url
        parsed = urlsplit(relay_url)
        # The relay URL contains an opaque signed query (not necessarily
        # key=value pairs). Preserve it byte-for-byte; rebuilding it with
        # urlencode invalidates the signature.
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query + "&retryTime=0"
        else:
            path += "?retryTime=0"
        connect_url = relay_url
        cdn_origin = ""
        if use_cdn:
            connect_url = str(relay_info.get("cdnUrl") or "")
            if not connect_url:
                raise RuntimeError("pre-connection response has no cdnUrl")
            if "://" not in connect_url:
                connect_url = "https://" + connect_url
            origin_url = str(relay_info.get("relayBusinessUrl") or "")
            if origin_url:
                cdn_origin = urlsplit(
                    origin_url if "://" in origin_url else "https://" + origin_url
                ).netloc
            cdn_parsed = urlsplit(connect_url)
            host = cdn_parsed.hostname
        else:
            host = parsed.hostname
        if not host:
            raise RuntimeError("relayUrl has no hostname")
        port = (urlsplit(connect_url).port or 443) if use_cdn else (parsed.port or 443)
        player_id = self.last_cipc_player_id
        if not player_id:
            raise RuntimeError("CIPC identity is missing for the relay")
        print(
            "Relay route: "
            f"host={host}; path={parsed.path or '/'}; "
            f"query_present={'yes' if parsed.query else 'no'}"
        )
        headers = {
            "Host": host,
            "User-Agent": f"Client={APP_NAME} Android {APP_VERSION}/1.3",
            "Keep-Relay": "3600",
            "Accept": "*/*",
            "Content-Type": "multipart/mixed;boundary=--client-stream-boundary--",
            "Content-Length": "21474836470" if use_cdn else "9223372036854775807",
            "X-token": relay_token,
            # The Android client uses the camera/client model here, rather
            # than the operating-system name.
            "X-Client-Model": str(device.get("deviceModel") or "C200"),
            "X-Client-UUID": player_id,
            "X-Redirect-Times": "0",
            # Tapo's PullMode enum is COMMON=0, PRE_CONNECTION=1,
            # DIRECT_FORWARDING=2.  The official client uses 2 for a fresh
            # live-stream relay request; 0 leaves the tunnel idle.
            "X-Pull-Mode": "2",
            "X-Version": "2.0",
            "X-Arrive-Latency": "0",
            "Connection": "Keep-alive",
        }
        if cdn_origin:
            headers["X-CDN-Origin"] = cdn_origin
        elb_cookie = str(relay_info.get("elbCookie") or "")
        if elb_cookie:
            headers["Cookie"] = elb_cookie
        raw_request = (
            f"POST {path} HTTP/1.1\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in headers.items())
            + "\r\n"
        ).encode("ascii")
        context = self.verify_context
        with socket.create_connection((host, port), timeout=15) as plain_socket:
            with context.wrap_socket(plain_socket, server_hostname=host) as tls_socket:
                tls_socket.settimeout(15)
                tls_socket.sendall(raw_request)
                # Headers and the first multipart bytes are not guaranteed to
                # arrive in the same TLS record. Read until the HTTP header
                # terminator before parsing the response.
                received = bytearray()
                while b"\r\n\r\n" not in received and len(received) < 65536:
                    chunk = tls_socket.recv(8192)
                    if not chunk:
                        break
                    received.extend(chunk)
                first = bytes(received)
                header_end = first.find(b"\r\n\r\n")
                media_bytes = bytearray(
                    first[header_end + 4 :] if header_end >= 0 else b""
                )
                # Keep the probe bounded: the relay is a live stream, so a
                # normal read would otherwise never finish.
                tls_socket.settimeout(3)
                while len(media_bytes) < 65536:
                    try:
                        chunk = tls_socket.recv(min(8192, 65536 - len(media_bytes)))
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    media_bytes.extend(chunk)
                first = first[: header_end + 4] + bytes(media_bytes) if header_end >= 0 else first
        if not first:
            raise RuntimeError("relay closed the connection without a response")
        header_end = first.find(b"\r\n\r\n")
        header_bytes = first if header_end < 0 else first[:header_end]
        status_line = header_bytes.split(b"\r\n", 1)[0].decode("latin1", "replace")
        header_text = header_bytes.decode("latin1", "replace").lower()
        content_type = next(
            (line.split(":", 1)[1].strip() for line in header_text.split("\r\n")
             if line.startswith("content-type:")),
            "<missing>",
        )
        body = first[header_end + 4:] if header_end >= 0 else b""
        detail = ""
        if body:
            try:
                body_json = json.loads(body.decode("utf-8", "replace"))
                if isinstance(body_json, dict):
                    detail = (
                        f"; error={body_json.get('errorCode', body_json.get('error_code', '<n/d>'))}"
                        f"; message={body_json.get('msg', body_json.get('message', '<n/a>'))}"
                    )
            except json.JSONDecodeError:
                pass
        print(
            f"Relay handshake ({'CDN' if use_cdn else 'node'}): {status_line}; content-type={content_type}; "
            f"received-bytes={len(first)}; payload={len(body)}{detail}"
        )
        if body:
            print(
                "Initial relay payload: "
                f"boundary={'yes' if b'--device-stream-boundary--' in body else 'no'}; "
                f"hex={body[:24].hex()}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{PRODUCT_NAME}: test cloud Tapo")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS verification; use only when the system reports a certificate error",
    )
    parser.add_argument(
        "--relay-test",
        action="store_true",
        help="also test legacy relay negotiation for the first camera",
    )
    parser.add_argument(
        "--cipc-test",
        action="store_true",
        help="test modern CIPC pre-connection for the first camera",
    )
    parser.add_argument(
        "--stream-test",
        action="store_true",
        help="open the CIPC relay and read only the first response packet",
    )
    parser.add_argument(
        "--cdn-test",
        action="store_true",
        help="test the CIPC relay HTTPS CDN path",
    )
    parser.add_argument(
        "--fresh-login",
        action="store_true",
        help="ignore the saved session and force a new login/MFA",
    )
    args = parser.parse_args()

    client = TapoCloud(insecure=args.insecure)
    base_url = ""
    devices = None

    if not args.fresh_login:
        session = load_session()
        if session:
            base_url = str(session.get("base_url") or LOGIN_URL)
            client.token = str(session["token"])
            print("Tapo session found; trying to reuse it without MFA.")
            try:
                devices = client.list_cameras(base_url)
                print("Session reused successfully.")
            except RuntimeError:
                print("Session expired or invalid; starting a new login.")
                client.token = ""
                base_url = ""

    if devices is None:
        print(f"{PRODUCT_NAME}: the password will not be saved.")
        username = input("Tapo email/account: ").strip()
        password = getpass.getpass("Tapo account password: ")
        if not username or not password:
            print("Email and password are required.", file=sys.stderr)
            return 2
        try:
            base_url, result = client.login(username, password)
            result = client.complete_mfa(base_url, username, password, result)
            token = result.get("token")
            if not token:
                raise RuntimeError(
                    "login response has no token; additional verification may be required"
                )
            client.token = str(token)
            saved_path = save_session(base_url, result)
            print(f"Cloud login succeeded. Session saved to {saved_path}")
            devices = client.list_cameras(base_url)
        except (RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if not devices:
        print("Login succeeded, but no cameras were returned.")
        return 0
    print(f"Cameras found: {len(devices)}")
    for index, device in enumerate(devices, start=1):
        status = device.get("status")
        print(
            f"{index}. {device.get('alias') or device.get('deviceName') or '<unnamed>'} "
            f"| model={device.get('deviceModel', '<n/a>')} "
            f"| account_status={status if status is not None else '<n/a>'} "
            f"| id={mask_id(device.get('deviceId'))}"
        )
    if args.relay_test:
        try:
            client.probe_legacy_relay(base_url, devices[0])
        except RuntimeError as exc:
            print(f"Relay probe failed: {exc}", file=sys.stderr)
            return 1
    if args.cipc_test:
        try:
            client.probe_cipc_preconnection(base_url, devices[0])
        except RuntimeError as exc:
            print(f"CIPC probe failed: {exc}", file=sys.stderr)
            return 1
    if args.stream_test:
        try:
            client.probe_cipc_stream(base_url, devices[0])
        except (RuntimeError, OSError, ssl.SSLError) as exc:
            print(f"Stream handshake failed: {exc}", file=sys.stderr)
            return 1
    if args.cdn_test:
        try:
            client.probe_cipc_stream(base_url, devices[0], use_cdn=True)
        except (RuntimeError, OSError, ssl.SSLError) as exc:
            print(f"CDN handshake failed: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
