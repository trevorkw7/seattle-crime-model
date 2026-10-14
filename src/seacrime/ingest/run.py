"""CLI: snapshot one or more sources.

    python -m seacrime.ingest.run spd_crime fire_911
    python -m seacrime.ingest.run all
"""

from __future__ import annotations

import argparse
import logging

from ..config import load_config
from .socrata import snapshot
from .sources import SOCRATA_SOURCES


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="+")
    ap.add_argument("--config")
    ap.add_argument("--max-rows", type=int)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    names = args.sources
    if names == ["all"]:
        names = list(SOCRATA_SOURCES) + ["weather", "events", "gtfs", "boundaries"]
    for name in names:
        if name in SOCRATA_SOURCES:
            p = snapshot(name, SOCRATA_SOURCES[name], cfg.raw_dir, max_rows=args.max_rows)
            logging.info("snapshot written: %s", p)
        elif name == "weather":
            from .weather import fetch_weather

            fetch_weather(cfg)
        elif name == "events":
            from .events import fetch_events

            fetch_events(cfg)
        elif name == "gtfs":
            from .gtfs import fetch_gtfs

            fetch_gtfs(cfg)
        elif name == "boundaries":
            from .boundaries import fetch_boundaries

            fetch_boundaries(cfg)
        else:
            raise SystemExit(f"unknown source {name}")


if __name__ == "__main__":
    main()
