#!/usr/bin/env python3
"""GTK/WebKit desktop shell for the cloud-only Tapo viewer."""

from __future__ import annotations

import argparse
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gio, GLib, Gtk, WebKit2  # noqa: E402

from tapo_cloud_app import PRODUCT_NAME, CloudAppContext, Handler  # noqa: E402

ROOT = Path(__file__).resolve().parent
ICON_PATH = ROOT / "tapo-cloud-viewer.png"
if not ICON_PATH.exists():
    ICON_PATH = ROOT / "linux" / "tapo-cloud-viewer.png"


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
    args = parser.parse_args()
    application = TapoDesktop(
        port=args.port,
        fresh_login=args.fresh_login,
    )
    return application.run([])


if __name__ == "__main__":
    raise SystemExit(main())
