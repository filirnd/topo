# Changelog

All notable changes to Topo are documented here.

## [Unreleased]

- Improvements and fixes for the Linux desktop application.
- More camera models and additional Tapo features will be evaluated with
  community testing.

## [0.1.0] - 2026-10-14

### Added

- Live cloud streaming for multiple Tapo cameras in one desktop window.
- Tapo account login with MFA through the app notification or email code.
- Reusable session support and logout.
- Per-camera privacy mode controls.
- Pan and tilt controls with selectable movement steps.
- Per-camera fullscreen mode, including double-click activation.
- Fullscreen command drawer that keeps controls separate from the video.
- Connection checks, loading states, error states, and retry support.
- Linux desktop launcher and Topo application icon.
- Dependency-aware Linux installer for Ubuntu/Debian and Fedora/RHEL.
- Self-hosted Flatpak repository publication through GitHub Pages.

### Notes

- Tapo C200 is currently the tested camera model.
- The cloud integration uses undocumented Tapo endpoints and may require
  updates if Tapo changes its service.
- Audio, recording, snapshots, and desktop motion notifications are not yet
  implemented.
