"""Offline (re-)organize: build the library + playlists from the local cache.

Useful when the files were already pulled (they live under <workdir>/stage) and
the device is no longer connected, or to re-run the organization step with
different options without touching the phone.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import _core as core


def organize_offline(workdir: Path, out: Path, by_artist: bool = True) -> dict:
    """Organize staged files into out/Music and write out/Playlists/*.m3u8.

    Returns a small summary dict. Prefers the device DB (cached under workdir)
    for names and playlists, falling back to each file's embedded tags.
    """
    stage = workdir / "stage"
    db = workdir / "MediaLibrary.sqlitedb"
    library = out / "Music"
    pdir = out / "Playlists"
    library.mkdir(parents=True, exist_ok=True)
    pdir.mkdir(parents=True, exist_ok=True)

    if not stage.is_dir():
        raise FileNotFoundError(f"no staged files at {stage}; run `isnarf pull` first")

    meta = core.db_metadata(db) if db.exists() else {}

    # Build a Track for every staged audio file.
    tracks = []
    for root, _dirs, files in os.walk(stage):
        for f in files:
            if Path(f).suffix.lower() in core.AUDIO_EXTS:
                lp = Path(root) / f
                remote = str(lp.relative_to(stage))
                t = core.Track(remote=remote, local=lp)
                core.read_tags(lp, t)
                m = meta.get(remote.lstrip("/"))
                if m:
                    t.title = m["title"] or t.title
                    t.artist = m["artist"] or t.artist
                    t.album = m["album"] or t.album
                tracks.append(t)

    # Organize with de-collision so every distinct file is preserved.
    used: set[str] = set()
    for t in tracks:
        rel = core.dest_for(t, by_artist=by_artist)
        dst = library / rel
        stem, suf, i = dst.stem, dst.suffix, 2
        while str(dst).lower() in used:
            dst = dst.with_name(f"{stem} ({i}){suf}")
            i += 1
        used.add(str(dst).lower())
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(t.local, dst)
        t.local = dst

    # Playlists from the cached DB.
    n_pl = n_links = 0
    if db.exists():
        by_remote = {t.remote.lstrip("/"): t for t in tracks}
        used_names: set[str] = set()
        for pl in core.extract_playlists(db):
            base = core.safe(pl.name)
            fname, i = base, 2
            while fname.lower() in used_names:
                fname = f"{base} ({i})"
                i += 1
            used_names.add(fname.lower())
            with (pdir / f"{fname}.m3u8").open("w", encoding="utf-8") as fh:
                fh.write("#EXTM3U\n")
                for rp in pl.tracks:
                    t = by_remote.get(rp.lstrip("/"))
                    if t and t.local:
                        rel = os.path.relpath(t.local, pdir)
                        fh.write(f"#EXTINF:-1,{t.artist} - {t.title}\n{rel}\n")
                        n_links += 1
            n_pl += 1

    return {"tracks": len(tracks), "playlists": n_pl, "links": n_links,
            "out": str(out)}


def cmd_organize(args) -> int:
    summary = organize_offline(Path(args.workdir), Path(args.out).expanduser(),
                               by_artist=not args.flat)
    print(f"Organized {summary['tracks']} tracks, wrote {summary['playlists']} "
          f"playlists ({summary['links']} links) under {summary['out']}")
    return 0
