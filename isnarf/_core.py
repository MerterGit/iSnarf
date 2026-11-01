#!/usr/bin/env python3
"""
iSnarf core — download music files and playlists from an iOS device over USB.

Built on pymobiledevice3 (Apple File Conduit / lockdown). No jailbreak required;
this reads the media partition that Apple exposes to a trusted host, the same
surface Finder/iTunes use to sync.

Developed and tested against iOS 16.

This module holds the device-side engine (connect, pull, resume/retry, DB
parsing, tag-based organization). The user-facing CLI lives in isnarf.cli,
which also adds offline re-organize and macOS Music.app import.

Audio file names on the device are obfuscated (e.g. Music/F03/ABCD.m4a); the
real title/artist/album live in each file's embedded tags and in the library
database. iSnarf uses the device database as the authoritative source for names
and playlist membership, and each file's embedded tags as a fallback.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import sqlite3
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Silence cosmetic teardown/import noise from the networking stack. None of it
# affects behavior: urllib3 warns about LibreSSL at import, and pymobiledevice3
# aborts its sockets at close without awaiting StreamWriter.wait_closed(), which
# leaves harmless pending tasks when the event loop shuts down.
warnings.filterwarnings("ignore", message=r".*OpenSSL 1\.1\.1\+")
warnings.filterwarnings("ignore", message=r"coroutine '.*wait_closed' was never awaited")
logging.getLogger("asyncio").setLevel(logging.CRITICAL)

from pymobiledevice3.lockdown import create_using_usbmux  # noqa: E402
from pymobiledevice3.services.afc import AfcService  # noqa: E402

# AFC (com.apple.afc) is rooted at /var/mobile/Media on the device, so all
# remote paths below are relative to that media root.
AUDIO_EXTS = {".m4a", ".m4p", ".m4b", ".mp3", ".aac", ".wav", ".aif", ".aiff", ".flac", ".alac"}
DB_CANDIDATES = [
    "iTunes_Control/iTunes/MediaLibrary.sqlitedb",
    "iTunes_Control/iTunes/MediaLibrary.sqlitedb3",
]
# Roots under the media partition where audio typically lives.
MUSIC_ROOTS = ["iTunes_Control/Music", "iTunes_Control/iTunes_Control/Music", "Purchases"]

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe(name: str, fallback: str = "Unknown") -> str:
    """Make a string safe to use as a single path component."""
    name = (name or "").strip()
    name = ILLEGAL.sub("_", name)
    name = name.rstrip(". ")
    return name[:180] or fallback


async def connect(serial: Optional[str]) -> AfcService:
    lockdown = await create_using_usbmux(serial=serial)
    afc = AfcService(lockdown=lockdown)
    await afc.connect()
    # Keep the lockdown reference so we can close its SSL transport explicitly;
    # otherwise it's torn down after the event loop closes and asyncio logs a
    # spurious "Bad file descriptor / Event loop is closed" traceback at exit.
    afc._owned_lockdown = lockdown
    return afc


async def disconnect(afc: AfcService) -> None:
    try:
        await afc.aclose()
    except Exception:
        pass
    lockdown = getattr(afc, "_owned_lockdown", None)
    if lockdown is not None:
        try:
            await lockdown.close()
        except Exception:
            pass
    # Let the transports' close callbacks run before asyncio.run() closes the loop.
    await asyncio.sleep(0)


async def afc_size(afc: AfcService, path: str) -> Optional[int]:
    """Device-side file size, or None if it can't be read. Used to resume."""
    try:
        st = await afc.stat(path)
        return int(st.get("st_size") or 0)
    except Exception:
        return None


async def reconnect(afc: AfcService, serial: Optional[str], pause: float = 2.0) -> AfcService:
    """Close a dead AFC session and open a fresh one (retrying a few times)."""
    await disconnect(afc)
    last = None
    for _ in range(6):
        await asyncio.sleep(pause)
        try:
            return await connect(serial)
        except Exception as e:
            last = e
    raise RuntimeError(f"could not reconnect to device: {last}")


async def afc_exists(afc: AfcService, path: str) -> bool:
    try:
        return await afc.exists(path)
    except Exception:
        return False


async def find_db(afc: AfcService) -> Optional[str]:
    for c in DB_CANDIDATES:
        if await afc_exists(afc, c):
            return c
    return None


async def find_music_roots(afc: AfcService) -> list[str]:
    roots = []
    for r in MUSIC_ROOTS:
        if await afc_exists(afc, r):
            roots.append(r)
    return roots


async def list_audio(afc: AfcService, root: str) -> list[str]:
    """Return every audio file path (relative to media root) under `root`."""
    found: list[str] = []
    async for dirpath, _dirs, files in afc.walk(root):
        for f in files:
            if Path(f).suffix.lower() in AUDIO_EXTS:
                found.append(f"{dirpath}/{f}" if not dirpath.endswith("/") else f"{dirpath}{f}")
    return found


# --------------------------------------------------------------------------- info
async def cmd_info(args) -> int:
    afc = await connect(args.serial)
    try:
        info = await afc.get_device_info()
        print("Connected over AFC. Device info:")
        for k, v in (info or {}).items():
            print(f"  {k}: {v}")
        db = await find_db(afc)
        print(f"\nLibrary database: {db or 'NOT FOUND'}")
        roots = await find_music_roots(afc)
        print(f"Music roots found: {roots or 'NONE'}")
        total = 0
        for r in roots:
            n = len(await list_audio(afc, r))
            print(f"  {r}: {n} audio files")
            total += n
        print(f"Total audio files visible: {total}")
        if not roots:
            print("\nNo music roots found. If this device has music, run `schema` "
                  "and `pull --scan-all` so we can locate where it's stored.")
        return 0
    finally:
        await disconnect(afc)


# ------------------------------------------------------------------------- schema
async def cmd_schema(args) -> int:
    afc = await connect(args.serial)
    try:
        db = await find_db(afc)
        if not db:
            print("MediaLibrary.sqlitedb not found on device.", file=sys.stderr)
            return 2
        out = Path(args.workdir)
        out.mkdir(parents=True, exist_ok=True)
        local = out / "MediaLibrary.sqlitedb"
        # Pull the DB and its write-ahead log / shared-memory siblings if present,
        # so sqlite sees a consistent snapshot.
        await afc.pull(db, str(local))
        for ext in ("-wal", "-shm"):
            if await afc_exists(afc, db + ext):
                await afc.pull(db + ext, str(local) + ext)
        print(f"Pulled library DB -> {local}\n")
        con = sqlite3.connect(f"file:{local}?mode=ro", uri=True)
        cur = con.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        print(f"{len(tables)} tables:\n")
        for t in tables:
            try:
                n = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except Exception:
                n = "?"
            cols = [c[1] for c in cur.execute(f'PRAGMA table_info("{t}")')]
            print(f"  {t}  ({n} rows)")
            print(f"      cols: {', '.join(cols)}")
        con.close()
        return 0
    finally:
        await disconnect(afc)


# --------------------------------------------------------------------------- pull
@dataclass
class Track:
    remote: str                      # path on device (relative to media root)
    local: Optional[Path] = None     # where we copied it
    title: str = ""
    artist: str = ""
    album: str = ""
    protected: bool = False          # DRM (.m4p / FairPlay) — may not play elsewhere


@dataclass
class Playlist:
    name: str
    tracks: list[str] = field(default_factory=list)   # remote paths, in order


def read_tags(path: Path, track: Track) -> None:
    """Fill title/artist/album from the file's own embedded metadata."""
    try:
        import mutagen
        mf = mutagen.File(str(path), easy=True)
        if mf is not None:
            def first(key):
                v = mf.get(key)
                return (v[0] if v else "") if isinstance(v, list) else (v or "")
            track.title = first("title")
            track.artist = first("artist") or first("albumartist")
            track.album = first("album")
    except Exception:
        pass
    if path.suffix.lower() == ".m4p":
        track.protected = True


def dest_for(track: Track, by_artist: bool) -> str:
    title = safe(track.title or Path(track.remote).stem)
    ext = Path(track.remote).suffix.lower()
    if by_artist:
        return f"{safe(track.artist)}/{safe(track.album)}/{title}{ext}"
    return f"{title}{ext}"


def extract_playlists(db_path: Path) -> list[Playlist]:
    """
    Reconstruct user playlists from MediaLibrary.sqlitedb (verified on iOS 16).

    Layout:
      * base_location(base_location_id -> path)  e.g. 3840 -> 'iTunes_Control/Music/F00'
      * item_extra(item_pid -> location)         e.g. 'ZEKY.mp3'  (relative to the
        item's base_location, found via item.base_location_id)
      * container                                = the playlists
      * container_item(container_pid, item_pid, position) = membership, ordered

    A "real" user playlist is distinguished_kind=0, is_hidden=0 and
    is_ignorable_itunes_playlist=0; that excludes system containers such as
    'Playback History' and the hidden 'trashPhone' entries. Each track path is
    base_location.path + '/' + item_extra.location. Guarded so that on an
    unrecognized schema (another iOS version) it returns [] rather than raising.
    """
    playlists: list[Playlist] = []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    try:
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"base_location", "item", "item_extra", "container",
                "container_item"} <= tables:
            return []

        base = {r["base_location_id"]: (r["path"] or "")
                for r in cur.execute("SELECT base_location_id, path FROM base_location")}

        loc: dict[int, str] = {}
        for r in cur.execute("""
                SELECT i.item_pid AS pid, i.base_location_id AS bl, ie.location AS location
                FROM item i JOIN item_extra ie ON i.item_pid = ie.item_pid
                WHERE ie.location IS NOT NULL"""):
            bp = base.get(r["bl"], "")
            loc[r["pid"]] = f"{bp.rstrip('/')}/{r['location']}" if bp else r["location"]

        # Inner query needs its own cursor: reusing `cur` would reset the outer
        # container iteration after the first row.
        icur = con.cursor()
        for cr in cur.execute("""
                SELECT container_pid AS pid, name FROM container
                WHERE distinguished_kind = 0
                  AND is_hidden = 0
                  AND is_ignorable_itunes_playlist = 0
                  AND name IS NOT NULL
                ORDER BY name"""):
            paths = [loc[row["item_pid"]]
                     for row in icur.execute(
                         "SELECT item_pid FROM container_item WHERE container_pid=? "
                         "ORDER BY position", (cr["pid"],))
                     if row["item_pid"] in loc]
            if paths:
                playlists.append(Playlist(name=cr["name"], tracks=paths))
    except Exception as e:
        print(f"  (playlist parse skipped: {e})", file=sys.stderr)
    finally:
        con.close()
    return playlists


def db_metadata(db_path: Path) -> dict[str, dict]:
    """
    Map each file's device path -> {title, artist, album} from the library DB.

    The on-device database is the authoritative source for names (every item
    carries a title), so we prefer it over each file's embedded tags, which are
    sometimes sparse. Keyed by the same path form list_audio() produces, i.e.
    base_location.path + '/' + item_extra.location. Returns {} on an
    unrecognized schema so the caller falls back to embedded tags.
    """
    meta: dict[str, dict] = {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute("""
                SELECT bl.path AS base, ie.location AS loc, ie.title AS title,
                       ia.item_artist AS artist, al.album AS album
                FROM item i
                JOIN item_extra ie   ON i.item_pid = ie.item_pid
                JOIN base_location bl ON i.base_location_id = bl.base_location_id
                LEFT JOIN item_artist ia ON i.item_artist_pid = ia.item_artist_pid
                LEFT JOIN album al       ON i.album_pid = al.album_pid
                WHERE ie.location IS NOT NULL"""):
            base = (r["base"] or "").rstrip("/")
            key = f"{base}/{r['loc']}" if base else r["loc"]
            meta[key] = {"title": (r["title"] or "").strip(),
                         "artist": (r["artist"] or "").strip(),
                         "album": (r["album"] or "").strip()}
    except Exception as e:
        print(f"  (db metadata skipped: {e})", file=sys.stderr)
    finally:
        con.close()
    return meta


async def cmd_pull(args) -> int:
    from tqdm import tqdm

    out = Path(args.out).expanduser()
    (out / "Music").mkdir(parents=True, exist_ok=True)
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)

    afc = await connect(args.serial)
    try:
        roots = await find_music_roots(afc)
        if args.scan_all or not roots:
            # Fall back to scanning the whole media partition for audio.
            roots = [""]
        print(f"Scanning: {roots or ['<media root>']}")
        remote_files: list[str] = []
        for r in roots:
            remote_files += await list_audio(afc, r or ".")
        remote_files = sorted(set(remote_files))
        print(f"Found {len(remote_files)} audio files on device.")
        if not remote_files:
            print("Nothing to pull.")
            return 0
        available = set(remote_files)

        # 0) Pull the library DB first: it supplies authoritative names and the
        #    playlist definitions we may be filtering on.
        db = await find_db(afc)
        local_db: Optional[Path] = None
        meta: dict[str, dict] = {}
        all_playlists: list[Playlist] = []
        if db:
            local_db = work / "MediaLibrary.sqlitedb"
            await afc.pull(db, str(local_db))
            for ext in ("-wal", "-shm"):
                if await afc_exists(afc, db + ext):
                    await afc.pull(db + ext, str(local_db) + ext)
            meta = db_metadata(local_db)
            all_playlists = extract_playlists(local_db)
            print(f"Loaded metadata for {len(meta)} items and "
                  f"{len(all_playlists)} playlists from library DB.")

        # Optionally restrict this run to a single playlist's tracks.
        selected_playlists = all_playlists
        if args.playlist:
            match = [p for p in all_playlists if p.name.strip().lower() == args.playlist.strip().lower()]
            if not match:
                match = [p for p in all_playlists if args.playlist.strip().lower() in p.name.strip().lower()]
            if not match:
                print(f"No playlist matching {args.playlist!r}. Available:", file=sys.stderr)
                for p in all_playlists:
                    print(f"  - {p.name} ({len(p.tracks)} tracks)", file=sys.stderr)
                return 2
            selected_playlists = match
            # Pull only files that belong to the selected playlist(s), preserving order.
            wanted, seen = [], set()
            for p in selected_playlists:
                for rp in p.tracks:
                    key = rp.lstrip("/")
                    if key in available and key not in seen:
                        seen.add(key)
                        wanted.append(key)
            remote_files = wanted
            print(f"Playlist filter: {', '.join(p.name for p in selected_playlists)} "
                  f"-> {len(remote_files)} files.")

        if args.limit:
            remote_files = remote_files[:args.limit]
            print(f"Limiting to first {len(remote_files)} files (--limit).")

        # 1) Copy every file to a staging tree mirroring device paths, resiliently.
        #    Resume skips files already fully copied (size matches the device), and
        #    a dropped USB/AFC session is reconnected and the file retried, so one
        #    disconnect can't abort the remaining thousands of files. Names come
        #    from the DB when available, else from the file's own embedded tags.
        stage = work / "stage"
        tracks: list[Track] = []
        failed: list[str] = []
        n_pulled = n_resumed = 0
        pbar = tqdm(remote_files, desc="Copying", unit="file")
        for rp in pbar:
            lp = stage / rp
            lp.parent.mkdir(parents=True, exist_ok=True)

            # Resume: skip if the staged file already matches the device size.
            if lp.exists():
                rs = await afc_size(afc, rp)
                if rs and lp.stat().st_size == rs:
                    n_resumed += 1
                    rp_ok = True
                else:
                    rp_ok = False
            else:
                rp_ok = False

            if not rp_ok:
                rp_ok = False
                for attempt in range(4):
                    try:
                        await afc.pull(rp, str(lp), progress_bar=False)
                        rp_ok = True
                        n_pulled += 1
                        break
                    except Exception as e:
                        if attempt < 3:
                            pbar.write(f"  reconnecting after: {e}")
                            try:
                                afc = await reconnect(afc, args.serial)
                            except Exception as ce:
                                pbar.write(f"  reconnect failed: {ce}")
                                break
                        else:
                            pbar.write(f"  skip {rp}: {e}")
                if not rp_ok:
                    failed.append(rp)
                    continue

            t = Track(remote=rp, local=lp)
            read_tags(lp, t)            # embedded tags (fallback)
            m = meta.get(rp.lstrip("/"))
            if m:                        # DB values win where present
                t.title = m["title"] or t.title
                t.artist = m["artist"] or t.artist
                t.album = m["album"] or t.album
            tracks.append(t)

        pbar.close()
        print(f"Copied {n_pulled} new, resumed {n_resumed} already-present, "
              f"{len(failed)} failed of {len(remote_files)} files.")
        if failed:
            print(f"  {len(failed)} files failed after retries; re-run the same "
                  f"command to resume and fetch just those.")

        # 2) Lay out a human-readable library from embedded tags. Each distinct
        #    source file gets a distinct destination: when two tracks map to the
        #    same Artist/Album/Title path, later ones get a " (2)" suffix so no
        #    file is dropped (every device track is preserved).
        library = out / "Music"
        used_dests: set[str] = set()
        for t in tracks:
            rel = dest_for(t, by_artist=not args.flat)
            dst = library / rel
            stem, suf, i = dst.stem, dst.suffix, 2
            while str(dst).lower() in used_dests:
                dst = dst.with_name(f"{stem} ({i}){suf}")
                i += 1
            used_dests.add(str(dst).lower())
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(t.local, dst)
            t.local = dst  # point at final location for playlist links
        print(f"Organized {len(tracks)} tracks under {library}")
        prot = [t for t in tracks if t.protected]
        if prot:
            print(f"  note: {len(prot)} file(s) are DRM-protected (.m4p) and may "
                  f"not play without the authorizing Apple ID.")

        # 3) Playlists from the library DB (loaded in step 0, possibly filtered).
        if local_db:
            pls = selected_playlists
            if pls:
                pdir = out / "Playlists"
                pdir.mkdir(exist_ok=True)
                # Exact device-path -> pulled track, so obfuscated basenames that
                # recur across F00..F49 folders can't cross-match.
                by_remote = {t.remote.lstrip("/"): t for t in tracks}
                used_names: set[str] = set()
                for pl in pls:
                    # De-collide playlist filenames (duplicate names exist).
                    base = safe(pl.name)
                    fname, i = base, 2
                    while fname.lower() in used_names:
                        fname = f"{base} ({i})"
                        i += 1
                    used_names.add(fname.lower())
                    m3u = pdir / f"{fname}.m3u8"
                    matched = 0
                    with m3u.open("w", encoding="utf-8") as fh:
                        fh.write("#EXTM3U\n")
                        for rp in pl.tracks:
                            t = by_remote.get(rp.lstrip("/"))
                            if t and t.local:
                                rel = os.path.relpath(t.local, pdir)
                                fh.write(f"#EXTINF:-1,{t.artist} - {t.title}\n{rel}\n")
                                matched += 1
                    print(f"  playlist: {pl.name} ({matched}/{len(pl.tracks)} tracks)"
                          f" -> {m3u.name}")
                print(f"Wrote {len(pls)} playlist(s) to {pdir}")
            else:
                print("No playlists reconstructed (schema not recognized). "
                      "Run `schema` and share output to finalize playlist support.")
        else:
            print("Library DB not found; exported tracks only, no playlists.")

        print(f"\nDone. Output: {out}")
        return 0
    finally:
        await disconnect(afc)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--serial", help="device UDID (default: first device)")
    p.add_argument("--workdir", default=str(Path(__file__).parent / ".cache"),
                   help="staging/cache dir")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="connect and locate music + library DB")
    sub.add_parser("schema", help="pull MediaLibrary.sqlitedb and dump its schema")

    pp = sub.add_parser("pull", help="download audio files + playlists")
    pp.add_argument("--out", default=str(Path.home() / "iPhoneMusic"),
                    help="output folder (default: ~/iPhoneMusic)")
    pp.add_argument("--flat", action="store_true",
                    help="flat Music/ folder instead of Artist/Album/")
    pp.add_argument("--scan-all", action="store_true",
                    help="scan the whole media partition for audio")
    pp.add_argument("--playlist", default=None,
                    help="only pull files in this playlist (exact or substring match)")
    pp.add_argument("--limit", type=int, default=0,
                    help="only pull the first N files (0 = all; for testing)")

    args = p.parse_args()
    handler = {"info": cmd_info, "schema": cmd_schema, "pull": cmd_pull}[args.cmd]

    async def runner():
        rc = await handler(args)
        # pymobiledevice3 aborts its sockets without awaiting wait_closed(),
        # leaving background close tasks pending. Give them a moment to settle
        # so asyncio doesn't print "Task was destroyed but it is pending" when
        # the loop closes. Bounded so a stuck close can't hang us.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=1.0)
        return rc

    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
