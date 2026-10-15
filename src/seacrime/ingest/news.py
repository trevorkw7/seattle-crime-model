"""Timestamped local news items (RSS/Atom feeds, Google News RSS, GDELT DOC API).

Every item carries three times:

* ``published_at_utc`` — the publisher's (or GDELT's ``seendate``) timestamp;
* ``fetched_at_utc`` — when *we* first downloaded it (first-seen time is kept
  across appends);
* ``available_at_utc`` — the leakage-safe availability time used by default,
  equal to ``fetched_at_utc``.

**Leakage rule.** A news item may only feed a forecast issued at ``t`` if
``available_at_utc <= t``. Items we did not observe live (anything that was
already old when first fetched, and any GDELT window backfill) may only be used
in explicitly labelled *backfill* experiments, with availability
``published_at_utc + margin`` (:data:`BACKFILL_MARGIN`) — see
:func:`usable_news`. Feeds only expose their latest items, so real history
accumulates only by running :func:`fetch_news` repeatedly (e.g. hourly).

:func:`extract_news_signals` turns items into structured signals: a keyword /
gazetteer extractor (always available, deterministic) and an optional LLM
extractor (Anthropic API; skipped gracefully when no ``ANTHROPIC_API_KEY`` or
SDK is present).
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd

from ..config import Config
from ._context_common import HttpError, RawSnapshot, http_get, session, write_parquet

log = logging.getLogger(__name__)

RSS_FEEDS: dict[str, str] = {
    "king5_local": "https://www.king5.com/feeds/syndication/rss/news/local",
    "komo_local": "https://komonews.com/news/local.rss",
    "kiro7_local": "https://www.kiro7.com/arc/outboundfeeds/rss/category/news/local/?outputType=xml",
    "the_stranger": "https://www.thestranger.com/feed",
    "capitol_hill_seattle_blog": "https://www.capitolhillseattle.com/feed/",
    "publicola": "https://publicola.com/feed/",
    "my_ballard": "https://myballard.com/feed/",
    "south_seattle_emerald": "https://southseattleemerald.org/feed/",
    "seattle_met": "https://www.seattlemet.com/feed",
    "west_seattle_blog": "https://westseattleblog.com/feed/",
    "mynorthwest": "https://mynorthwest.com/feed/",
    "seattle_times_local": "https://www.seattletimes.com/seattle-news/feed/",
    "google_news_seattle": "https://news.google.com/rss/search?q=Seattle&hl=en-US&gl=US&ceid=US:en",
}
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_QUERY = "seattle sourcelang:english"
GDELT_PAUSE_S = 6.0  # GDELT asks for <= 1 request / 5 s
BACKFILL_MARGIN = pd.Timedelta(hours=1)
LIVE_MAX_AGE = pd.Timedelta(hours=6)  # older than this when first fetched => not observed "live"
TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|cmpid|ocid|taid|ref)$", re.I)
DEFAULT_LLM_MODEL = "claude-opus-5-5"
ATOM = "{http://www.w3.org/2005/Atom}"

NEWS_COLUMNS = [
    "item_id",
    "url",
    "title",
    "summary",
    "source",
    "outlet",
    "published_at_utc",
    "fetched_at_utc",
    "last_seen_at_utc",
    "available_at_utc",
    "backfill_available_at_utc",
    "observed_live",
    "first_snapshot_id",
]


# --------------------------------------------------------------------------- parsing (pure)


def canonical_url(url: str) -> str:
    """Lower-case host, drop fragment and tracking parameters, strip trailing slash."""
    u = urlsplit(str(url).strip())
    query = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)])
    path = u.path.rstrip("/") or "/"
    return urlunsplit((u.scheme.lower() or "https", u.netloc.lower(), path, query, ""))


def item_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()[:20]


def strip_html(text: str | None, limit: int = 1000) -> str | None:
    if not text:
        return None
    t = re.sub(r"<[^>]+>", " ", text)
    t = re.sub(r"\s+", " ", html.unescape(t)).strip()
    return t[:limit] or None


def _parse_time(text: str | None) -> pd.Timestamp | None:
    if not text:
        return None
    try:
        return pd.Timestamp(email.utils.parsedate_to_datetime(text.strip())).tz_convert("UTC")
    except (TypeError, ValueError, IndexError):
        pass
    try:
        ts = pd.Timestamp(text.strip())
        return ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
    except (TypeError, ValueError):
        return None


def parse_feed(content: bytes, source: str, fetched_at: datetime) -> pd.DataFrame:
    """Parse RSS 2.0 or Atom bytes into news rows (no network)."""
    root = ET.fromstring(content)
    rows: list[dict[str, Any]] = []
    for it in root.iter("item"):
        src = it.find("source")
        rows.append(
            {
                "url": (it.findtext("link") or it.findtext("guid") or "").strip(),
                "title": strip_html(it.findtext("title"), 400),
                "summary": strip_html(it.findtext("description")),
                "published": it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"),
                "outlet": src.text.strip() if src is not None and src.text else source,
            }
        )
    for e in root.iter(f"{ATOM}entry"):
        link = next((lk.get("href") for lk in e.findall(f"{ATOM}link") if lk.get("rel") in (None, "alternate")), None)
        rows.append(
            {
                "url": (link or e.findtext(f"{ATOM}id") or "").strip(),
                "title": strip_html(e.findtext(f"{ATOM}title"), 400),
                "summary": strip_html(e.findtext(f"{ATOM}summary") or e.findtext(f"{ATOM}content")),
                "published": e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"),
                "outlet": source,
            }
        )
    return _finalize(rows, source, fetched_at)


def parse_gdelt_artlist(payload: dict, fetched_at: datetime, source: str = "gdelt_doc") -> pd.DataFrame:
    rows = [
        {
            "url": a.get("url", ""),
            "title": strip_html(a.get("title"), 400),
            "summary": None,
            "published": a.get("seendate"),  # GDELT first-seen time, 'YYYYMMDDTHHMMSSZ'
            "outlet": a.get("domain") or source,
        }
        for a in payload.get("articles", []) or []
    ]
    return _finalize(rows, source, fetched_at)


def _finalize(rows: list[dict[str, Any]], source: str, fetched_at: datetime) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["url", "title", "summary", "published", "outlet"])
    df = df[df["url"].fillna("").str.startswith("http")].copy()
    fetched = pd.Timestamp(fetched_at).tz_convert("UTC") if pd.Timestamp(fetched_at).tzinfo else pd.Timestamp(fetched_at, tz="UTC")
    df["item_id"] = df["url"].map(item_id)
    df["source"] = source
    df["published_at_utc"] = pd.to_datetime(df["published"].map(_parse_time), utc=True)
    df["fetched_at_utc"] = fetched
    return add_availability(df.drop(columns=["published"]))


def add_availability(df: pd.DataFrame) -> pd.DataFrame:
    """Derive availability columns from published/fetched times (see module docstring)."""
    df = df.copy()
    df["last_seen_at_utc"] = df.get("last_seen_at_utc", df["fetched_at_utc"])
    df["available_at_utc"] = df["fetched_at_utc"]
    df["backfill_available_at_utc"] = df["published_at_utc"] + BACKFILL_MARGIN
    age = df["fetched_at_utc"] - df["published_at_utc"]
    df["observed_live"] = age.notna() & (age <= LIVE_MAX_AGE)
    return df


def merge_news(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Append + dedupe by canonical URL, keeping the *first-seen* version of each item."""
    if existing is None or existing.empty:
        allf = new.copy()
    else:
        allf = pd.concat([existing, new], ignore_index=True)
    allf = allf.sort_values("fetched_at_utc", kind="stable")
    last_seen = allf.groupby("item_id")["fetched_at_utc"].transform("max")
    pub = allf.groupby("item_id")["published_at_utc"].transform("first")
    allf = allf.assign(last_seen_at_utc=last_seen.where(last_seen.notna(), allf["fetched_at_utc"]), published_at_utc=pub)
    first = allf.drop_duplicates("item_id", keep="first")
    first = add_availability(first.drop(columns=["available_at_utc", "backfill_available_at_utc", "observed_live"], errors="ignore"))
    first["last_seen_at_utc"] = last_seen.loc[first.index]
    return first.reindex(columns=NEWS_COLUMNS).sort_values("published_at_utc").reset_index(drop=True)


def usable_news(items: pd.DataFrame, issued_at: pd.Timestamp | str, mode: str = "strict") -> pd.DataFrame:
    """Items a forecast issued at ``issued_at`` may use.

    * ``strict`` (default, production & honest backtests): ``available_at_utc <= issued_at``.
    * ``backfill`` (labelled historical experiments only): items not observed
      live may be used from ``published_at_utc + BACKFILL_MARGIN``; the returned
      frame carries ``availability_basis`` so results can be labelled.
    """
    t = pd.Timestamp(issued_at)
    t = t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")
    if mode == "strict":
        out = items[items["available_at_utc"] <= t].copy()
        out["availability_basis"] = "fetched_at"
        return out
    if mode != "backfill":
        raise ValueError(f"unknown mode {mode!r}")
    avail = items["available_at_utc"].where(items["observed_live"], items["backfill_available_at_utc"])
    avail = avail.fillna(items["available_at_utc"])
    out = items[avail <= t].copy()
    out["availability_basis"] = out["observed_live"].map({True: "fetched_at", False: "backfill_published_plus_margin"}).astype(str)
    return out


# --------------------------------------------------------------------------- signal extraction

FLAG_PATTERNS: dict[str, str] = {
    "protest": r"\b(?:protest\w*|demonstrat\w*|rall(?:y|ies)|marchers|walkout|sit-in)\b",
    "parade": r"\bparades?\b",
    "festival": r"\b(?:festivals?|fest|street fair|block party|concerts?)\b",
    "closure": r"\b(?:clos(?:ed|es|ing|ures?)|detours?|lane restrictions?|shut(?:s|ting)? down)\b",
    "power_outage": r"\b(?:power outages?|outages?|without power|blackout)\b",
    "shooting": r"\b(?:shootings?|shot|gunfire|shooter)\b",
    "fire": r"\b(?:fires?|blaze|arson)\b",
    "police_activity": r"\b(?:police|arrest(?:ed|s)?|SPD|officers?)\b",
    "sports_event": r"\b(?:Mariners|Seahawks|Sounders|Kraken|Huskies|Reign FC|Seattle Storm|World Cup)\b",
    "severe_weather": r"\b(?:windstorm|snow(?:storm)?|heat wave|atmospheric river|flood(?:ing)?|smoke)\b",
}
_FLAG_RE = {k: re.compile(v, re.I) for k, v in FLAG_PATTERNS.items()}


# Neighborhood Map Atlas names that are mostly *not* neighborhood mentions in news text
# (directions, surnames, presidents, generic words) — dropped for precision.
AMBIGUOUS_HOODS = {
    "atlantic",
    "broadway",
    "bryant",
    "cascade",
    "mann",
    "minor",
    "stevens",
    "northeast",
    "northwest",
    "north central",
    "brighton",
    "riverview",
    "seaview",
    "windermere",
    "dunlap",
    "roosevelt",
    "genesee",
}


def load_neighborhood_gazetteer(cfg: Config) -> list[str]:
    """Neighborhood names from data/processed/neighborhoods.parquet (written by boundaries)."""
    p = cfg.processed_dir / "neighborhoods.parquet"
    if not p.exists():
        return []
    n = pd.read_parquet(p, columns=["l_hood", "s_hood"])
    names = {x.strip() for x in set(n["l_hood"].dropna()) | set(n["s_hood"].dropna()) if isinstance(x, str)}
    return sorted(x for x in names if len(x) > 3 and x.lower() not in AMBIGUOUS_HOODS)


def keyword_signals(items: pd.DataFrame, neighborhoods: Iterable[str] = ()) -> pd.DataFrame:
    """Deterministic flags + neighborhood mentions from title + summary."""
    text = (items["title"].fillna("") + " " + items["summary"].fillna("")).astype(str)
    out = pd.DataFrame({"item_id": items["item_id"].values}, index=items.index)
    for k, rx in _FLAG_RE.items():
        out[f"flag_{k}"] = text.str.contains(rx)
    hoods = sorted(set(neighborhoods), key=len, reverse=True)
    if hoods:
        hood_re = re.compile(r"\b(" + "|".join(re.escape(h) for h in hoods) + r")\b", re.I)
        canon = {h.lower(): h for h in hoods}
        out["neighborhoods"] = text.map(lambda t: "|".join(sorted({canon.get(m.lower(), m) for m in hood_re.findall(t)})) or None)
    else:
        out["neighborhoods"] = None
    out["mentions_seattle"] = text.str.contains(r"\bSeattle\b", case=False) | out["neighborhoods"].notna()
    out["extraction_method"] = "keyword"
    return out


LLM_PROMPT = """You extract structured, forecast-relevant event information from Seattle news items.
For each item return an object with keys: item_id, event_type (one of protest, parade, festival,
sports, concert, road_closure, power_outage, shooting, fire, police_activity, weather, other, none),
location_text (string or null), neighborhood (Seattle neighborhood name or null), expected_crowd
(integer or null, only if stated), event_start_local (ISO 8601 local time or null, only if stated),
is_future_event (true/false). Use only the text given. Reply with a JSON array only.

Items:
{items}"""


def make_anthropic_llm(model: str = DEFAULT_LLM_MODEL) -> Callable[[str], str] | None:
    """Return a prompt->text callable backed by the Anthropic API, or None if unavailable."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.info("ANTHROPIC_API_KEY not set; LLM news extraction skipped")
        return None
    try:
        import anthropic  # optional dependency
    except ImportError:
        log.info("anthropic SDK not installed; LLM news extraction skipped")
        return None
    client = anthropic.Anthropic()

    def call(prompt: str) -> str:
        resp = client.beta.messages.create(
            model=model,
            max_tokens=8000,
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default", "output_config": {"effort": "low"}},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            return ""
        return "".join(b.text for b in resp.content if b.type == "text")

    return call


def _parse_llm_json(text: str) -> list[dict[str, Any]]:
    a, b = text.find("["), text.rfind("]")
    if a < 0 or b <= a:
        return []
    try:
        data = json.loads(text[a : b + 1])
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict) and d.get("item_id")]


def extract_news_signals(
    items_df: pd.DataFrame,
    llm: Callable[[str], str] | str | None = None,
    neighborhoods: Iterable[str] = (),
    batch_size: int = 25,
) -> pd.DataFrame:
    """Structured signals per news item.

    ``llm=None`` -> keyword/gazetteer extraction only (pure, deterministic).
    ``llm="auto"`` -> use :func:`make_anthropic_llm` when ``ANTHROPIC_API_KEY``
    and the ``anthropic`` SDK are available, else fall back to keywords.
    ``llm=<callable>`` -> any prompt->text function (e.g. a test stub).
    LLM fields (``llm_*``) are added on top of the keyword columns; a failed
    batch keeps its keyword-only result.
    """
    base = keyword_signals(items_df, neighborhoods)
    fn = make_anthropic_llm() if llm == "auto" else llm
    if fn is None or not callable(fn) or items_df.empty:
        return base.reset_index(drop=True)
    results: dict[str, dict[str, Any]] = {}
    for i in range(0, len(items_df), batch_size):
        chunk = items_df.iloc[i : i + batch_size]
        payload = json.dumps(
            [{"item_id": r.item_id, "title": r.title, "summary": r.summary} for r in chunk.itertuples()],
            ensure_ascii=False,
        )
        try:
            for d in _parse_llm_json(fn(LLM_PROMPT.format(items=payload))):
                results[str(d["item_id"])] = d
        except Exception as e:  # network / API errors: keep keyword result
            log.warning("LLM extraction failed for batch %d: %s", i // batch_size, e)
    for key in ("event_type", "location_text", "neighborhood", "expected_crowd", "event_start_local", "is_future_event"):
        base[f"llm_{key}"] = base["item_id"].map(lambda x, k=key: results.get(x, {}).get(k))
    base.loc[base["item_id"].isin(results), "extraction_method"] = "llm+keyword"
    return base.reset_index(drop=True)


# --------------------------------------------------------------------------- fetchers


def _fetch_rss(snap: RawSnapshot, sess: Any) -> list[pd.DataFrame]:
    frames = []
    for name, url in RSS_FEEDS.items():
        try:
            r = http_get(sess, url, timeout=45, retries=2, backoff_s=5)
        except HttpError as e:
            snap.blocked(f"rss {name}", url, e.status, e.detail)
            continue
        try:
            df = parse_feed(r.content, name, snap.fetched_at)
        except ET.ParseError as e:
            snap.blocked(f"rss {name}", url, r.status_code, f"unparseable feed: {e}")
            continue
        snap.save(f"rss/{name}.xml", r.content, url, rows=len(df))
        frames.append(df)
        log.info("news %s: %d items", name, len(df))
    return frames


def _fetch_gdelt(snap: RawSnapshot, sess: Any, backfill_days: int) -> list[pd.DataFrame]:
    """Most recent 24 h, plus optional day-by-day backfill (GDELT keeps ~3 months)."""
    end = pd.Timestamp(snap.fetched_at).floor("h")
    windows = [(end - pd.Timedelta(days=d + 1), end - pd.Timedelta(days=d)) for d in range(max(1, backfill_days))]
    frames = []
    for a, b in windows:
        params = {
            "query": GDELT_QUERY,
            "mode": "artlist",
            "format": "json",
            "maxrecords": 250,
            "sort": "datedesc",
            "startdatetime": a.strftime("%Y%m%d%H%M%S"),
            "enddatetime": b.strftime("%Y%m%d%H%M%S"),
        }
        payload, status, detail = None, None, ""
        for attempt in range(3):
            time.sleep(GDELT_PAUSE_S * (1 + 2 * attempt))
            try:
                r = http_get(sess, GDELT_DOC, params, timeout=60, retries=1)
            except HttpError as e:  # HTTP 429 when rate-limited
                status, detail = e.status, e.detail
                continue
            try:
                payload = r.json()
                break
            except ValueError:  # ... or HTTP 200 with a plain-text notice
                status, detail = r.status_code, r.text
        if payload is None:
            snap.blocked(f"gdelt {a:%Y-%m-%d}", GDELT_DOC, status, detail)
            continue
        df = parse_gdelt_artlist(payload, snap.fetched_at)
        snap.save(f"gdelt/artlist_{a:%Y%m%d%H}.json", r.content, GDELT_DOC, params, rows=len(df))
        frames.append(df)
    return frames


def fetch_news(cfg: Config, gdelt_backfill_days: int = 7) -> Path:
    """Fetch all feeds once and append to ``data/processed/news_items.parquet``.

    Run repeatedly (e.g. hourly) to accumulate a leakage-safe archive.
    """
    snap = RawSnapshot(cfg.raw_dir, "news")
    sess = session({"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"})
    frames = _fetch_rss(snap, sess) + _fetch_gdelt(snap, session(), gdelt_backfill_days)
    new = pd.concat([f for f in frames if not f.empty], ignore_index=True) if frames else pd.DataFrame(columns=NEWS_COLUMNS)
    new["first_snapshot_id"] = snap.snapshot_id
    path = cfg.processed_dir / "news_items.parquet"
    existing = pd.read_parquet(path) if path.exists() else None
    merged = merge_news(existing, new)
    write_parquet(merged, path)
    sig = extract_news_signals(merged, None, load_neighborhood_gazetteer(cfg))
    write_parquet(sig, cfg.processed_dir / "news_signals.parquet")
    snap.finish(
        license="Headlines/summaries/links only, for research; copyright remains with publishers. GDELT: open, cite GDELT Project.",
        row_counts={
            "fetched_this_run": int(len(new)),
            "unique_this_run": int(new["item_id"].nunique()) if len(new) else 0,
            "total_after_merge": int(len(merged)),
            "by_source_this_run": {k: int(v) for k, v in new["source"].value_counts().items()} if len(new) else {},
        },
        outputs={"news_items": str(path), "news_signals": str(cfg.processed_dir / "news_signals.parquet")},
    )
    log.info("news: %d new rows, %d total items", len(new), len(merged))
    return path


__all__ = [
    "fetch_news",
    "parse_feed",
    "parse_gdelt_artlist",
    "merge_news",
    "usable_news",
    "extract_news_signals",
    "keyword_signals",
    "make_anthropic_llm",
    "canonical_url",
]
