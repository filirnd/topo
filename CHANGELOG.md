# Changelog

All notable changes to Topo are documented here.

## [0.1.7] - 2026-10-26

### Fixed

- Fixed live stream freezes when enabling audio.
- Prevented unnecessary video decoder processes from blocking the relay.
- Improved audio relay reconnect handling.
- Prevented repeated live stream retries during normal playback.
- Restored per-camera volume control visibility when audio is enabled.

## [0.1.6] - 2026-10-26

### Fixed

- Improved live preview stability during long-running sessions.
- Added automatic recovery when a stream stops receiving frames.
- Prevented stale cloud relay responses from freezing the live video.
- Improved reconnection handling after temporary relay interruptions.

## [0.1.5] - 2026-10-21

### Added

- Low-latency live playback enabled by default for supported cameras.

### Improved

- Reduced live-preview delay and improved stream startup reliability.
- Improved handling of temporarily unavailable cameras and stream retries.
- Added automatic recovery when the Linux interface process stops responding.

## [0.1.4] - 2026-10-15

### Added

- SD-card recording calendar and 24-hour timeline for each camera.
- Full playback of selected SD-card segments through the integrated video player.
- Audio playback in SD recordings by decoding the C200 G.711 relay track.
- Download of individual SD-card recording clips as MP4 files.
- A readable recording-segment list with larger click targets for dense timelines.
- Native save dialogs for recording downloads on Linux and Windows.
- Per-camera live audio through the cloud relay, enabled by an explicit audio button.
- Per-camera live audio volume control.
- Camera-local timezone handling for recording dates and clip times.

### Fixed

- Reused the prepared recording for downloads instead of requesting the same
  clip from the cloud relay a second time.
- Removed the 10-second playback limit from selected recording segments and
  extended relay waiting time according to the requested clip duration.

### Notes

- The release includes the Linux desktop installer and the Windows x64
  installer workflow.

## [0.1.3] - 2026-10-14

### Fixed

- Stabilized GitHub Pages deployment after stale deployments could block a new
  publication.
- Added the Windows release workflow and installation documentation.

## [0.1.2] - 2026-10-14

### Fixed

- Kept the fullscreen Controls button centered when the command drawer opens.

## [0.1.1] - 2026-10-14

### Added

- Info button linking directly to the Topo project website.

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

### Fixed

- Fixed consecutive privacy mode enable/disable operations getting stuck after
  the first state change.
- Recreated the affected camera's cloud media relay after privacy changes so
  video resumes reliably when privacy is disabled again.
- Refreshed only the camera whose privacy state changed instead of rebuilding
  every camera card.

### Notes

- Tapo C200 is currently the tested camera model.
- The cloud integration uses undocumented Tapo endpoints and may require
  updates if Tapo changes its service.
- Live preview audio, snapshots, and desktop motion notifications are not yet implemented.
