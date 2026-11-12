"""
Every SCAN_EVERY: turn gnome-pipelines' rt_films into rt launch and shutdown triggers. The rt_reviews source that
writes it runs without end, so there is no run to hook onto, as cs2_scanner hooks onto cs2_upcoming's.

rt_films has a row per change in a film's markets: open or closed, its close, and every venue's strikes (`markets`,
the exchange security ids of each strike's YES and NO sides). For the newest row of each film:

- launch while it is open, its markets close within LAUNCH_BEFORE_CLOSE (Kalshi closes them about three days after
  release, so this is about ten days before it), its Tomatometer counts fewer than RT_KNOWN reviews and its first
  review wave hasn't started: the session (rt_review_wave) waits for the wave, trades it and ends itself. A film RT
  already scores had its wave, seen by too few of the outlets to count as one, and the market knows its score; the
  session wouldn't trade it (its max_rt_reviews), so it isn't launched. The wave rule mirrors the session's (WAVE_REVIEWS seen within
  WAVE_CLUSTER, counting only reviews seen within WAVE_LAG of being published), so a film whose wave has passed isn't
  launched just to end at once;
- shut down once the venues no longer list it (closed) or its close has passed, for CLOSED_FOR after.

Each scan resends the same triggers; the launcher's dedup and its active-session check make the repeats no-ops, and the
rt rule won't relaunch a film whose session stopped. Markets are sorted so the same strikes are always the same
trigger.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import boto3
import pandas as pd
from gnomepy.data import DatasetStore

from launcher.queues import send_trigger

logger = logging.getLogger(__name__)

FILMS = "rt_films"
REVIEWS = "rt_reviews"
SCORES = "rt_scores"
RULE_TYPE = "rt"
LAUNCH_BEFORE_CLOSE = pd.Timedelta(days=13)
CLOSED_FOR = pd.Timedelta(days=2)
WAVE_REVIEWS = 3
WAVE_CLUSTER = pd.Timedelta(hours=6)
WAVE_LAG = pd.Timedelta(hours=6)
# rt_review_wave's default max_rt_reviews.
RT_KNOWN = 5
CLOSED, PAST_CLOSE = "closed", "past_close"


def latest(rows: pd.DataFrame, key: str) -> pd.DataFrame:
    return rows.sort_values("first_seen_at", kind="stable").drop_duplicates(key, keep="last")


def wave_started(reviews: pd.DataFrame) -> bool:
    """Whether one film's reviews (newest row per URL) hold its first wave."""
    known = reviews[reviews["is_review"].fillna(False).astype(bool)]
    lag = pd.to_datetime(known["first_seen_at"], utc=True) - pd.to_datetime(known["published_at"], utc=True)
    seen = sorted(pd.to_datetime(known["first_seen_at"], utc=True)[lag.isna() | (lag <= WAVE_LAG)])
    return any(seen[i + WAVE_REVIEWS - 1] - seen[i] <= WAVE_CLUSTER for i in range(len(seen) - WAVE_REVIEWS + 1))


def triggers(films: pd.DataFrame, reviews: pd.DataFrame, now: pd.Timestamp,
             scores: pd.DataFrame | None = None) -> list[tuple[str, dict]]:
    """("launch" | "shutdown", trigger data) for each film's newest row; scores is each film's newest Tomatometer."""
    by_film = {fid: rows for fid, rows in reviews.groupby("film_id")} if len(reviews) else {}
    counted = ({r.film_id: int(r.liked) + int(r.not_liked) for r in scores.itertuples()}
               if scores is not None and len(scores) else {})
    out = []
    for f in films.itertuples():
        markets = sorted(json.loads(f.markets) if isinstance(f.markets, str) and f.markets else [],
                         key=lambda m: (m["venue"], m["strike"]))
        data = {"film_id": f.film_id, "title": f.title, "rt_slug": f.rt_slug if isinstance(f.rt_slug, str) else None,
                "markets": markets}
        close = pd.Timestamp(f.close) if not pd.isna(f.close) else None
        if not f.open:
            if now - pd.Timestamp(f.first_seen_at) <= CLOSED_FOR:
                out.append(("shutdown", {**data, "reason": CLOSED}))
        elif close is not None and close <= now:
            if now - close <= CLOSED_FOR:
                out.append(("shutdown", {**data, "reason": PAST_CLOSE}))
        elif close is not None and close - now <= LAUNCH_BEFORE_CLOSE and markets \
                and counted.get(f.film_id, 0) < RT_KNOWN:
            film_reviews = by_film.get(f.film_id)
            if film_reviews is None or not wave_started(film_reviews):
                out.append(("launch", data))
    return out


def handler(event, context):
    now = pd.Timestamp(datetime.now(timezone.utc))
    store = DatasetStore()
    # Every column, not a selection: reads follow the newest snapshot's schema, so a column added to the table (as
    # `markets` was) is missing until the source next writes, and selecting it would fail the scan.
    films = store.load(FILMS)
    if "markets" not in films.columns:
        films = films.assign(markets=None)
    reviews = store.load(REVIEWS, columns=["url", "film_id", "published_at", "first_seen_at", "is_review"])
    scores = store.load(SCORES, columns=["film_id", "liked", "not_liked"])
    found = triggers(latest(films, "film_id"), latest(reviews, "url") if len(reviews) else reviews, now, scores) \
        if len(films) else []
    queues = {"launch": os.environ["LAUNCHER_QUEUE_URL"], "shutdown": os.environ["SHUTDOWN_QUEUE_URL"]}
    sqs = boto3.client("sqs")
    for action, data in found:
        send_trigger(sqs, queues[action], RULE_TYPE, action, data, now.isoformat(), None)
    counts = {a: sum(1 for x, _ in found if x == a) for a in ("launch", "shutdown")}
    logger.info("rt scan: %d films, %s", len(films), counts)
    return counts
