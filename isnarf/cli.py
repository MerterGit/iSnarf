#!/usr/bin/env python3
"""iSnarf command-line interface.

Subcommands:
    info             Connect and report the device; locate music + library DB.
    schema           Pull MediaLibrary.sqlitedb and print its schema.
    pull             Copy audio off the device, organize it, rebuild playlists.
    organize         Re-organize already-pulled files offline (no device).
    import           Import an output dir into macOS Music.app (songs + playlists).
    import-playlist  Import a single .m3u8 into Music.app.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__
from . import _core as core
from .organize import cmd_organize
from .musicapp import cmd_import, cmd_import_playlist

DEFAULT_WORKDIR = str(Path.home() / ".cache" / "isnarf")
DEFAULT_OUT = str(Path.home() / "iPhoneMusic")

_ASYNC = {"info", "schema", "pull"}


def _run_async(handler, args) -> int:
    async def runner():
        rc = await handler(args)
        # pymobiledevice3 aborts its sockets without awaiting wait_closed();
        # let those close tasks settle so asyncio stays quiet at shutdown.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=1.0)
        return rc
    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="isnarf", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"iSnarf {__version__}")
    p.add_argument("--serial", help="device UDID (default: first device)")
    p.add_argument("--workdir", default=DEFAULT_WORKDIR,
                   help=f"staging/cache dir (default: {DEFAULT_WORKDIR})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="connect and locate music + library DB")
    sub.add_parser("schema", help="pull MediaLibrary.sqlitedb and dump its schema")

    pp = sub.add_parser("pull", help="download audio files + playlists")
    pp.add_argument("--out", default=DEFAULT_OUT, help=f"output folder (default: {DEFAULT_OUT})")
    pp.add_argument("--flat", action="store_true", help="flat Music/ folder instead of Artist/Album/")
    pp.add_argument("--scan-all", action="store_true", help="scan the whole media partition for audio")
    pp.add_argument("--playlist", default=None, help="only pull files in this playlist (exact or substring)")
    pp.add_argument("--limit", type=int, default=0, help="only pull the first N files (0 = all; for testing)")

    op = sub.add_parser("organize", help="re-organize already-pulled files offline")
    op.add_argument("--out", default=DEFAULT_OUT, help=f"output folder (default: {DEFAULT_OUT})")
    op.add_argument("--flat", action="store_true", help="flat Music/ folder instead of Artist/Album/")

    ip = sub.add_parser("import", help="[macOS] import an output dir into Music.app")
    ip.add_argument("--out", default=DEFAULT_OUT, help=f"output folder to import (default: {DEFAULT_OUT})")

    ipp = sub.add_parser("import-playlist", help="[macOS] import one .m3u8 into Music.app")
    ipp.add_argument("m3u8", help="path to the .m3u8 file")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "info": core.cmd_info, "schema": core.cmd_schema, "pull": core.cmd_pull,
        "organize": cmd_organize, "import": cmd_import,
        "import-playlist": cmd_import_playlist,
    }
    handler = handlers[args.cmd]
    if args.cmd in _ASYNC:
        return _run_async(handler, args)
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
