#!/usr/bin/env python3
"""GTK/WebKit desktop shell for the cloud-only Tapo viewer."""

from __future__ import annotations

import argparse
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2  # noqa: E402

try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify as LibNotify  # noqa: E402

    NOTIFY_AVAILABLE = True
except (ValueError, ImportError):
    LibNotify = None  # type: ignore[assignment]
    NOTIFY_AVAILABLE = False

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402

    APPINDICATOR_AVAILABLE = True
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator  # noqa: E402

        APPINDICATOR_AVAILABLE = True
    except (ValueError, ImportError):
        AppIndicator = None  # type: ignore[assignment]
        APPINDICATOR_AVAILABLE = False

from tapo_cloud_app import PRODUCT_NAME, CloudAppContext, Handler  # noqa: E402
from tapo_watch import AlertWatcher, WatchAlert, describe_alert  # noqa: E402

ROOT = Path(__file__).resolve().parent
ICON_PATH = ROOT / "tapo-cloud-viewer.png"
if not ICON_PATH.exists():
    ICON_PATH = ROOT / "linux" / "tapo-cloud-viewer.png"

WATCH_PREVIEW_SECONDS = 45
PIP_WIDTH = 420
PIP_HEIGHT = 280


class TapoDesktop(Gtk.Application):
    def __init__(self, *, port: int, fresh_login: bool):
        super().__init__(
            application_id="io.github.filirnd.topo",
            flags=0,
        )
        self.requested_port = port
        self.fresh_login = fresh_login
        self.context: CloudAppContext | None = None
        self.server: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.window: Gtk.ApplicationWindow | None = None
        self.webview: WebKit2.WebView | None = None
        self.webview_overlay: Gtk.Overlay | None = None
        self.recovery_label: Gtk.Label | None = None
        self.recovery_source_id = 0
        self.shutting_down = False

    def do_activate(self) -> None:  # noqa: N802 - GTK virtual method
        if self.window is not None:
            self.window.present()
            return
        try:
            self.context = CloudAppContext(self.fresh_login)
            handler_class = type("DesktopAppHandler", (Handler,), {"context": self.context})
            self.server = ThreadingHTTPServer(("127.0.0.1", self.requested_port), handler_class)
            port = self.server.server_address[1]
            self.server_thread = threading.Thread(
                target=self.server.serve_forever,
                name="tapo-desktop-http",
                daemon=True,
            )
            self.server_thread.start()
        except (OSError, RuntimeError) as exc:
            print(f"Failed to start {PRODUCT_NAME}: {exc}", file=sys.stderr)
            self.quit()
            return

        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title(PRODUCT_NAME)
        if ICON_PATH.exists():
            self.window.set_icon_from_file(str(ICON_PATH))
        self.window.set_default_size(1280, 820)
        self.window.connect("destroy", lambda *_args: self.quit())

        self.webview_overlay = Gtk.Overlay()
        self.recovery_label = Gtk.Label(label="Recovering Topo interface…")
        self.recovery_label.set_halign(Gtk.Align.CENTER)
        self.recovery_label.set_valign(Gtk.Align.CENTER)
        self.recovery_label.set_margin_start(24)
        self.recovery_label.set_margin_end(24)
        self.recovery_label.set_margin_top(24)
        self.recovery_label.set_margin_bottom(24)
        self.recovery_label.set_no_show_all(True)
        self.recovery_label.hide()
        self.webview_overlay.add_overlay(self.recovery_label)
        self.window.add(self.webview_overlay)
        self._create_webview(port)
        self.window.show_all()

    def _create_webview(self, port: int) -> None:
        if self.webview_overlay is None:
            return

        webview = WebKit2.WebView()
        settings = webview.get_settings()
        settings.set_property("enable-developer-extras", False)
        webview.get_context().connect("download-started", self._handle_download_started)
        webview.connect("decide-policy", self._handle_navigation)
        webview.connect("load-changed", self._handle_load_changed)
        webview.connect("web-process-terminated", self._handle_web_process_terminated)

        # The UI is served locally, but WebKit may otherwise reuse an older
        # cached index.html after an application update.
        webview.load_uri(f"http://127.0.0.1:{port}/?ui={uuid.uuid4().hex}")
        self.webview_overlay.add(webview)
        self.webview = webview
        webview.show()

    def _handle_load_changed(self, _webview, load_event) -> None:
        if load_event == WebKit2.LoadEvent.FINISHED and self.recovery_label is not None:
            self.recovery_label.hide()

    def _handle_web_process_terminated(self, _webview, *_args) -> None:
        """Recover the UI when WebKit's renderer process crashes or exits."""
        if self.shutting_down or self.recovery_source_id:
            return
        if self.recovery_label is not None:
            self.recovery_label.show()
        self.recovery_source_id = GLib.idle_add(self._recover_webview)

    def _recover_webview(self) -> bool:
        self.recovery_source_id = 0
        if self.shutting_down or self.webview_overlay is None or self.server is None:
            return GLib.SOURCE_REMOVE

        old_webview = self.webview
        if old_webview is not None:
            self.webview_overlay.remove(old_webview)
            old_webview.destroy()
            self.webview = None

        self._create_webview(self.server.server_address[1])
        return GLib.SOURCE_REMOVE

    def _handle_download_started(self, _context, download) -> None:
        download.connect("decide-destination", self._choose_download_destination)

    def _choose_download_destination(self, download, suggested_filename) -> bool:
        if self.window is None:
            download.cancel()
            return False
        dialog = Gtk.FileChooserDialog(
            title="Save Topo recording",
            parent=self.window,
            action=Gtk.FileChooserAction.SAVE,
            buttons=(
                "Cancel",
                Gtk.ResponseType.CANCEL,
                "Save",
                Gtk.ResponseType.ACCEPT,
            ),
        )
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(suggested_filename or "topo-recording.mp4")
        response = dialog.run()
        filename = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if not filename:
            download.cancel()
            return False
        download.set_destination(Gio.File.new_for_path(filename).get_uri())
        return True

    @staticmethod
    def _handle_navigation(_webview, decision, decision_type):
        """Open external project links in the user's default browser."""
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            action = decision.get_navigation_action()
            request = action.get_request() if action is not None else None
            uri = request.get_uri() if request is not None else ""
            if uri.startswith(("http://", "https://")) and not uri.startswith("http://127.0.0.1:"):
                Gio.AppInfo.launch_default_for_uri(uri, None)
                decision.ignore()
                return True
        decision.use()
        return False

    def do_shutdown(self) -> None:  # noqa: N802 - GTK virtual method
        self.shutting_down = True
        if self.recovery_source_id:
            GLib.source_remove(self.recovery_source_id)
            self.recovery_source_id = 0
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.webview = None
        self.webview_overlay = None
        self.recovery_label = None
        if self.context is not None:
            self.context.close()
            self.context = None
        Gtk.Application.do_shutdown(self)


class TapoWatchApp(Gtk.Application):
    """Background tray watcher with short-lived PiP live previews."""

    def __init__(
        self,
        *,
        port: int,
        fresh_login: bool,
        preview_seconds: int,
        test_alert: bool = False,
        headless: bool = False,
    ):
        super().__init__(
            application_id="io.github.filirnd.topo.watch",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.requested_port = port
        self.fresh_login = fresh_login
        self.preview_seconds = max(10, int(preview_seconds))
        self.test_alert = test_alert
        # Headless = no control window. Tray icon is still used when available.
        # Alert PiP windows still appear on events.
        self.headless = headless
        self.context: CloudAppContext | None = None
        self.server: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.watcher: AlertWatcher | None = None
        self.indicator = None
        self.fallback_window: Gtk.ApplicationWindow | None = None
        self.status_label: Gtk.Label | None = None
        self.pause_item = None
        self.pip_window: Gtk.ApplicationWindow | None = None
        self.pip_webview: WebKit2.WebView | None = None
        self.pip_close_source = 0
        self.paused = False
        self.shutting_down = False
        self._notify_ready = False

    def do_startup(self) -> None:  # noqa: N802
        Gtk.Application.do_startup(self)
        self.hold()
        if NOTIFY_AVAILABLE and LibNotify is not None:
            try:
                LibNotify.init("Topo Watcher")
                self._notify_ready = True
            except GLib.Error:
                self._notify_ready = False

    def do_activate(self) -> None:  # noqa: N802
        if self.server is not None:
            return
        try:
            self.context = CloudAppContext(self.fresh_login)
            if self.context.current_state() is None:
                raise RuntimeError(
                    "no saved Tapo session; open the main Topo app and log in once"
                )
            handler_class = type("WatchAppHandler", (Handler,), {"context": self.context})
            self.server = ThreadingHTTPServer(("127.0.0.1", self.requested_port), handler_class)
            self.server_thread = threading.Thread(
                target=self.server.serve_forever,
                name="topo-watch-http",
                daemon=True,
            )
            self.server_thread.start()
        except (OSError, RuntimeError) as exc:
            self._show_error_and_quit(str(exc))
            return

        self.watcher = AlertWatcher(on_alert=self._on_alert_thread)
        try:
            self.watcher.refresh_devices()
        except Exception as exc:  # noqa: BLE001
            self._show_error_and_quit(f"Unable to list cameras: {exc}")
            return
        self.watcher.start()
        self._setup_tray_or_fallback()
        self._set_status("Watching for doorbell and camera alerts")
        if self.test_alert:
            GLib.timeout_add_seconds(2, self._fire_test_alert)

    def _setup_tray_or_fallback(self) -> None:
        menu = Gtk.Menu()
        open_item = Gtk.MenuItem(label="Open dashboard")
        open_item.connect("activate", lambda *_: self._open_dashboard())
        menu.append(open_item)

        self.pause_item = Gtk.CheckMenuItem(label="Pause alerts")
        self.pause_item.set_active(False)
        self.pause_item.connect("toggled", self._toggle_paused)
        menu.append(self.pause_item)

        quit_item = Gtk.MenuItem(label="Quit Topo Watcher")
        quit_item.connect("activate", lambda *_: self.quit())
        menu.append(quit_item)
        menu.show_all()

        if APPINDICATOR_AVAILABLE and AppIndicator is not None:
            icon = str(ICON_PATH) if ICON_PATH.exists() else "camera-web"
            self.indicator = AppIndicator.Indicator.new(
                "io.github.filirnd.topo.watch",
                icon,
                AppIndicator.IndicatorCategory.APPLICATION_STATUS,
            )
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_title("Topo Watcher")
            self.indicator.set_menu(menu)
            return

        if self.headless:
            # No tray and no control window: stay alive via Gtk.Application.hold()
            # and only create a UI when an alert PiP is needed.
            self._set_status(
                "Headless watch mode active (no control window; alerts still pop up)"
            )
            return

        # Fallback when AppIndicator is unavailable (common inside Flatpak).
        self.fallback_window = Gtk.ApplicationWindow(application=self)
        self.fallback_window.set_title("Topo Watcher")
        self.fallback_window.set_default_size(320, 120)
        self.fallback_window.set_keep_above(True)
        if ICON_PATH.exists():
            self.fallback_window.set_icon_from_file(str(ICON_PATH))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        self.status_label = Gtk.Label(label="Watching for alerts")
        self.status_label.set_line_wrap(True)
        self.status_label.set_xalign(0.0)
        box.pack_start(self.status_label, True, True, 0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        open_button = Gtk.Button(label="Open dashboard")
        open_button.connect("clicked", lambda *_: self._open_dashboard())
        pause_button = Gtk.ToggleButton(label="Pause alerts")
        pause_button.connect(
            "toggled",
            lambda button: self._set_paused(button.get_active()),
        )
        quit_button = Gtk.Button(label="Quit")
        quit_button.connect("clicked", lambda *_: self.quit())
        buttons.pack_start(open_button, True, True, 0)
        buttons.pack_start(pause_button, True, True, 0)
        buttons.pack_start(quit_button, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.fallback_window.add(box)
        self.fallback_window.connect("delete-event", self._on_fallback_delete)
        self.fallback_window.show_all()

    def _on_fallback_delete(self, *_args) -> bool:
        # Keep the watcher alive; hide instead of quitting on window close.
        if self.fallback_window is not None:
            self.fallback_window.hide()
        return True

    def _toggle_paused(self, item) -> None:
        self._set_paused(bool(item.get_active()))

    def _set_paused(self, paused: bool) -> None:
        self.paused = paused
        if self.watcher is not None:
            self.watcher.set_paused(paused)
        if self.pause_item is not None and self.pause_item.get_active() != paused:
            self.pause_item.set_active(paused)
        self._set_status("Alerts paused" if paused else "Watching for doorbell and camera alerts")

    def _set_status(self, text: str) -> None:
        if self.status_label is not None:
            self.status_label.set_text(text)
        print(f"Topo Watcher: {text}", flush=True)

    def _open_dashboard(self) -> None:
        commands = [
            ["flatpak", "run", "io.github.filirnd.topo"],
            [sys.executable, str(ROOT / "tapo_desktop.py")],
        ]
        for command in commands:
            try:
                Gio.Subprocess.new(command, Gio.SubprocessFlags.NONE)
                return
            except GLib.Error:
                continue
        self._set_status("Unable to open the Topo dashboard")

    def _on_alert_thread(self, alert: WatchAlert) -> None:
        GLib.idle_add(self._handle_alert, alert)

    def _fire_test_alert(self) -> bool:
        """Open a one-shot PiP against the doorbell when present."""
        state = self.context.current_state() if self.context is not None else None
        if state is None or not state.devices:
            self._set_status("Test alert skipped: no devices")
            return GLib.SOURCE_REMOVE
        index = 0
        for candidate, device in enumerate(state.devices):
            if str(device.get("deviceType") or "") == "SMART.TAPODOORBELL":
                index = candidate
                break
        device = state.devices[index]
        self._handle_alert(
            WatchAlert(
                msg_id=f"test-{uuid.uuid4().hex}",
                time_ms=int(time.time() * 1000),
                msg_type="Ring",
                label="Test ring",
                title="Topo Watcher test alert",
                device_index=index,
                device_id=str(device.get("deviceId") or ""),
                device_name=str(
                    device.get("alias") or device.get("deviceName") or "Camera"
                ),
                device_model=str(device.get("deviceModel") or ""),
            )
        )
        return GLib.SOURCE_REMOVE

    def _handle_alert(self, alert: WatchAlert) -> bool:
        if self.shutting_down or self.paused:
            return GLib.SOURCE_REMOVE
        summary = describe_alert(alert)
        self._set_status(summary)
        self._notify(summary, alert.title or alert.label)
        self._show_pip(alert)
        return GLib.SOURCE_REMOVE

    def _notify(self, summary: str, body: str) -> None:
        if not self._notify_ready or LibNotify is None:
            return
        try:
            note = LibNotify.Notification.new(summary, body or None, None)
            if ICON_PATH.exists():
                note.set_hint("image-path", GLib.Variant("s", str(ICON_PATH)))
            note.set_urgency(LibNotify.Urgency.NORMAL)
            note.show()
        except GLib.Error as exc:
            print(f"Topo Watcher notification failed: {exc}", flush=True)

    def _show_pip(self, alert: WatchAlert) -> None:
        if self.server is None:
            return
        self._close_pip()
        port = self.server.server_address[1]
        query = (
            f"camera={alert.device_index}"
            f"&label={quote(alert.label)}"
            f"&name={quote(alert.device_name)}"
            f"&model={quote(alert.device_model)}"
            f"&ui={uuid.uuid4().hex}"
        )
        uri = f"http://127.0.0.1:{port}/watch?{query}"

        window = Gtk.ApplicationWindow(application=self)
        window.set_title(f"{alert.device_name} · {alert.label}")
        window.set_default_size(PIP_WIDTH, PIP_HEIGHT)
        window.set_keep_above(True)
        window.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        window.set_skip_taskbar_hint(False)
        if ICON_PATH.exists():
            window.set_icon_from_file(str(ICON_PATH))
        window.connect("destroy", lambda *_: self._on_pip_destroyed(window))

        webview = WebKit2.WebView()
        settings = webview.get_settings()
        settings.set_property("enable-developer-extras", False)
        webview.connect("decide-policy", self._handle_pip_navigation)
        webview.load_uri(uri)
        window.add(webview)
        window.show_all()
        self._position_pip(window)
        self.pip_window = window
        self.pip_webview = webview
        self.pip_close_source = GLib.timeout_add_seconds(
            self.preview_seconds,
            self._auto_close_pip,
        )

    def _position_pip(self, window: Gtk.ApplicationWindow) -> None:
        display = window.get_display()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return
        geometry = monitor.get_geometry()
        margin = 24
        x = geometry.x + geometry.width - PIP_WIDTH - margin
        y = geometry.y + geometry.height - PIP_HEIGHT - margin - 48
        window.move(max(geometry.x + margin, x), max(geometry.y + margin, y))

    def _handle_pip_navigation(self, _webview, decision, decision_type) -> bool:
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            action = decision.get_navigation_action()
            request = action.get_request() if action is not None else None
            uri = request.get_uri() if request is not None else ""
            if uri.startswith("topo://close"):
                decision.ignore()
                self._close_pip()
                return True
            if uri.startswith(("http://", "https://")) and not uri.startswith("http://127.0.0.1:"):
                decision.ignore()
                return True
        decision.use()
        return False

    def _auto_close_pip(self) -> bool:
        self.pip_close_source = 0
        self._close_pip()
        return GLib.SOURCE_REMOVE

    def _on_pip_destroyed(self, window: Gtk.ApplicationWindow) -> None:
        if self.pip_window is window:
            self.pip_window = None
            self.pip_webview = None
        if self.pip_close_source:
            GLib.source_remove(self.pip_close_source)
            self.pip_close_source = 0
        # Drop idle relay workers after the preview ends.
        if self.context is not None:
            state = self.context.current_state()
            if state is not None:
                for index in range(len(state.devices)):
                    state.reset_worker(index)

    def _close_pip(self) -> None:
        if self.pip_close_source:
            GLib.source_remove(self.pip_close_source)
            self.pip_close_source = 0
        window = self.pip_window
        self.pip_window = None
        self.pip_webview = None
        if window is not None:
            window.destroy()

    def _show_error_and_quit(self, message: str) -> None:
        print(f"Failed to start Topo Watcher: {message}", file=sys.stderr)
        if self.headless:
            self.quit()
            return
        dialog = Gtk.MessageDialog(
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Topo Watcher failed to start",
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
        self.quit()

    def do_shutdown(self) -> None:  # noqa: N802
        self.shutting_down = True
        self._close_pip()
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.context is not None:
            self.context.close()
            self.context = None
        if self._notify_ready and LibNotify is not None:
            LibNotify.uninit()
            self._notify_ready = False
        Gtk.Application.do_shutdown(self)


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{PRODUCT_NAME} desktop app")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="local HTTP port; 0 selects a free port automatically",
    )
    parser.add_argument(
        "--fresh-login",
        action="store_true",
        help="force a new Tapo login and MFA flow",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="run the battery-friendly tray alert watcher instead of the dashboard",
    )
    parser.add_argument(
        "--preview-seconds",
        type=int,
        default=WATCH_PREVIEW_SECONDS,
        help="how long alert previews stay open in watch mode",
    )
    parser.add_argument(
        "--watch-test",
        action="store_true",
        help="in watch mode, fire one test PiP alert shortly after startup",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="in watch mode, do not show a control window (service-friendly)",
    )
    args = parser.parse_args()
    if args.watch:
        application = TapoWatchApp(
            port=args.port,
            fresh_login=args.fresh_login,
            preview_seconds=args.preview_seconds,
            test_alert=args.watch_test,
            headless=args.headless,
        )
    else:
        application = TapoDesktop(
            port=args.port,
            fresh_login=args.fresh_login,
        )
    return application.run([])


if __name__ == "__main__":
    raise SystemExit(main())
