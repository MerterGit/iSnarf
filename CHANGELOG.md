# Changelog

All notable changes to iSnarf are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-01

Initial release.

### Added
- Pull audio files and playlists from an iOS device over USB (AFC via
  `pymobiledevice3`) — no jailbreak required.
- Resilient transfer: resume already-copied files and auto-reconnect through
  dropped USB/AFC sessions, so one disconnect can't abort the remaining files.
- Playlist reconstruction from the on-device `MediaLibrary.sqlitedb`.
- Database-authoritative track naming with each file's embedded tags as a
  fallback; de-collides duplicate file and playlist names.
- `organize` command to (re-)build the library and playlists offline from the
  local cache, without the device connected.
- macOS Music.app integration: `import` (whole library + all playlists) and
  `import-playlist` (a single `.m3u8`).
- Unified `isnarf` command-line interface:
  `info`, `schema`, `pull`, `organize`, `import`, `import-playlist`.

[0.1.0]: https://github.com/MerterGit/iSnarf/releases/tag/v0.1.0
