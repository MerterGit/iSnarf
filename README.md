# iSnarf 🎵📲

**Rescue your music and playlists from an iPhone or iPad — over USB, no jailbreak, no paid app.**

[![lint](https://github.com/MerterGit/iSnarf/actions/workflows/lint.yml/badge.svg)](https://github.com/MerterGit/iSnarf/actions/workflows/lint.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)

iSnarf pulls the audio files *and* playlists off an iOS device and either drops
them into a clean, tagged folder tree or imports them straight into macOS
**Music.app**. It was built and tested against **iOS 16**.

It talks to the device through Apple's own **AFC** (Apple File Conduit) service
via the excellent open-source
[`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3) — the same
trusted-host media interface Finder and iTunes use to sync. iSnarf is **not**
derived from any commercial app.

---

## Why it's not trivial

On the device, audio files have obfuscated names like
`iTunes_Control/Music/F03/ABCD.m4a`, and **playlist membership lives in a
SQLite database** (`MediaLibrary.sqlitedb`), not the filesystem. iSnarf:

1. Pulls the files over AFC, with **resume + auto-reconnect** so a dropped USB
   session doesn't lose the remaining thousands of files.
2. Reads the device's **library database** for authoritative titles/artists/
   albums and reconstructs every playlist.
3. Organizes everything into `Artist/Album/Title`, writing standard `.m3u8`
   playlists (de-colliding duplicate names).
4. Optionally imports the result into **macOS Music.app** (songs + playlists).

Each file's embedded tags are used as a fallback when the database is missing a
field, so names are reliable even on odd items.

## Install

```bash
git clone https://github.com/mertergit/iSnarf
cd iSnarf
python3 -m venv .venv && source .venv/bin/activate
pip install -e .          # or: pip install -r requirements.txt
```

Plug in the device and tap **Trust** (unlock it first).

## Usage

```bash
# 1. Verify the connection and locate music + the library DB
isnarf info

# 2. (optional) Dump the on-device library DB schema
isnarf schema

# 3. Pull everything into ~/iPhoneMusic (files + .m3u8 playlists)
isnarf pull
#   --out DIR        output folder (default: ~/iPhoneMusic)
#   --flat           one flat Music/ folder instead of Artist/Album/
#   --playlist NAME  only pull one playlist's tracks (exact or substring)
#   --limit N        only pull the first N files (handy for a smoke test)
#   --scan-all       scan the entire media partition for audio

# 4. (macOS) Import the pulled library into Music.app — all songs + playlists
isnarf import --out ~/iPhoneMusic

# Re-run the organize step offline (device unplugged), e.g. to switch layout:
isnarf organize --out ~/iPhoneMusic --flat

# Import a single playlist file:
isnarf import-playlist ~/iPhoneMusic/Playlists/"My Playlist.m3u8"
```

Output layout:

```
~/iPhoneMusic/
  Music/<Artist>/<Album>/<Title>.m4a
  Playlists/<Playlist Name>.m3u8
```

Interrupted mid-pull? Just run `isnarf pull` again — it skips files already
copied and fetches only what's missing.

## macOS Music.app notes

- First import triggers a one-time **Automation permission** prompt
  ("… wants to control Music") — approve it.
- With **Settings → Files → "Copy files to Media folder"** on, Music copies the
  songs into its own library, so the `~/iPhoneMusic` folder becomes disposable
  afterward. With it off, Music references the files in place — keep the folder.
- iSnarf only ever **reads** from the device and **adds** to your Music library;
  it never writes to or modifies the phone.

## Requirements

- Python 3.9+
- macOS for the `import` / `import-playlist` commands (they drive Music.app).
  `info` / `schema` / `pull` / `organize` work anywhere `pymobiledevice3` runs.
- A USB connection to a trusted iOS device.

## DRM

Old iTunes Store purchases (`.m4p`) are FairPlay-protected; iSnarf copies them
but they only play on a device/account authorized for them. The pull reports how
many it saw.

## Legal & ethical use

iSnarf is an interoperability tool for accessing **your own** media on **your
own** device — the equivalent of plugging in and copying your files off. Only
use it with music you own or otherwise have the right to copy, and with a device
you own or are authorized to access. You are responsible for complying with
applicable law and the terms of any content you handle.

## Credits

Built on [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3),
[`mutagen`](https://mutagen.readthedocs.io/), and
[`tqdm`](https://github.com/tqdm/tqdm). Licensed under the MIT License.
