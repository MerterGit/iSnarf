"""macOS Music.app integration (AppleScript).

Imports the organized library and reconstructed playlists into Music.app. When
Music's "Copy files to Media folder when adding to library" setting is on, the
files are copied into Music's own library; "Keep Media folder organized" then
re-files them by their tags.

macOS only. Importing adds tracks to the user's Music library; the first run
triggers a one-time macOS Automation permission prompt that must be approved.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
from pathlib import Path


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _osascript(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script], check=False,
                          capture_output=True, text=True)


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise SystemExit("Music.app import is macOS-only.")


def import_playlist(m3u8: Path) -> dict:
    """Create (replacing any same-named) a playlist from a .m3u8 and add its tracks.

    Delete-then-recreate keeps the result clean by construction and idempotent on
    re-runs, without a location-based dedupe (Music copies files asynchronously,
    so a track's location often isn't resolvable right after adding).
    """
    _require_macos()
    name = m3u8.stem
    base = m3u8.parent
    paths = [os.path.normpath(os.path.join(base, ln.strip()))
             for ln in m3u8.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    paths = [p for p in paths if os.path.isfile(p)]

    nm = _esc(name)
    lines = ['with timeout of 600 seconds', 'tell application "Music"',
             f'  repeat while (exists user playlist "{nm}")',
             f'    delete user playlist "{nm}"',
             '  end repeat',
             f'  set thePL to make new user playlist with properties {{name:"{nm}"}}']
    for p in paths:
        lines.append(f'  add (POSIX file "{_esc(p)}") to thePL')
    lines += ['  return (count of tracks of thePL)', 'end tell', 'end timeout']
    r = _osascript("\n".join(lines))
    return {"name": name, "sources": len(paths),
            "final": r.stdout.strip(), "rc": r.returncode, "err": r.stderr.strip()}


def add_library_folder(music_dir: Path) -> dict:
    """Add a whole folder tree to the Music library (imports songs + copies them)."""
    _require_macos()
    script = ('with timeout of 3600 seconds\n'
              'tell application "Music"\n'
              f'  add (POSIX file "{_esc(str(music_dir))}")\n'
              '  return (count of file tracks of library playlist 1)\n'
              'end tell\nend timeout')
    r = _osascript(script)
    return {"library_count": r.stdout.strip(), "rc": r.returncode, "err": r.stderr.strip()}


def import_all(root: Path) -> int:
    """Import an iSnarf output dir: all songs (root/Music) + all playlists."""
    _require_macos()
    music_dir = root / "Music"
    pl_dir = root / "Playlists"

    print(f"Adding library folder to Music: {music_dir}")
    res = add_library_folder(music_dir)
    print(f"  library file-track count now: {res['library_count']} rc={res['rc']}")
    if res["err"]:
        print("  STDERR:", res["err"], file=sys.stderr)

    m3us = sorted(glob.glob(str(pl_dir / "*.m3u8")))
    print(f"\nCreating {len(m3us)} playlists:")
    ok = 0
    for m in m3us:
        r = import_playlist(Path(m))
        tail = f"  {r['err']}" if r["err"] else ""
        print(f"  {r['name']!r}: {r['sources']} -> {r['final']} (rc={r['rc']}){tail}")
        if r["rc"] == 0:
            ok += 1
    print(f"\nDone: {ok}/{len(m3us)} playlists imported.")
    return 0 if ok == len(m3us) else 1


def cmd_import(args) -> int:
    return import_all(Path(args.out).expanduser())


def cmd_import_playlist(args) -> int:
    r = import_playlist(Path(args.m3u8).expanduser())
    print(f"{r['name']!r}: {r['sources']} sources -> {r['final']} tracks (rc={r['rc']})")
    if r["err"]:
        print("STDERR:", r["err"], file=sys.stderr)
    return r["rc"]
