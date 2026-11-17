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

The same rows also drive the `rt_maker` rule (gnomepy-research's rt_maker, a resting maker for Kalshi's liquidity
rewards), with the films' Kalshi strikes only. Every film open with Kalshi strikes and its close ahead, however far and
whatever its reviews (the maker pulls its quotes around them itself; the rewards pay every day a strike's program runs,
often weeks before release), is quoted, and the films are packed, in close order, first-fit into groups of at most
MAX_STRIKES strikes: a launch trigger per group. In RELAUNCH_HOURS a shutdown trigger carries every group, and the rule
stops any session holding something else, so the next scan relaunches it as its group now is.

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
MAKER_RULE_TYPE = "rt_maker"
MAKER_VENUE = "kalshi"
LAUNCH_BEFORE_CLOSE = pd.Timedelta(days=13)
CLOSED_FOR = pd.Timedelta(days=2)
WAVE_REVIEWS = 3
WAVE_CLUSTER = pd.Timedelta(hours=6)
WAVE_LAG = pd.Timedelta(hours=6)
# rt_review_wave's default max_rt_reviews.
RT_KNOWN = 5
CLOSED, PAST_CLOSE = "closed", "past_close"
# A session's Kalshi books all sit on one paper gateway (its one link to the venue), which caps a session at
# gnome-orchestrator's gateway.paper.max.streams (150; a strike is a stream) and gateway.session.max.open.orders (200; a
# strike rests a bid and an ask). The tighter of the two, at two orders a strike.
MAX_STRIKES = 100
# The UTC hours a maker session whose films or strikes changed is relaunched in, once a day: a relaunch costs every
# film of the session its resting orders and queue places for some minutes, and RT embargoes rarely lift then.
RELAUNCH_HOURS = range(9, 10)


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


def maker_groups(films: pd.DataFrame, now: pd.Timestamp) -> list[list[dict]]:
    """The rt_maker's films from each film's newest row, packed in close order first-fit into groups of at most
    MAX_STRIKES Kalshi strikes, never splitting a film."""
    found = []
    for f in films.itertuples():
        markets = sorted((m for m in (json.loads(f.markets) if isinstance(f.markets, str) and f.markets else [])
                          if m["venue"] == MAKER_VENUE), key=lambda m: m["strike"])
        close = pd.Timestamp(f.close) if not pd.isna(f.close) else None
        if not f.open or not markets or (close is not None and close <= now):
            continue
        found.append((close, {"film_id": f.film_id, "title": f.title,
                              "rt_slug": f.rt_slug if isinstance(f.rt_slug, str) else None,
                              "close": close.isoformat() if close is not None else None, "markets": markets}))
    found.sort(key=lambda x: (x[0] is None, x[0] if x[0] is not None else now, x[1]["film_id"]))
    groups: list[list[dict]] = []
    sizes: list[int] = []
    for _, film in found:
        n = len(film["markets"])
        if n > MAX_STRIKES:
            logger.warning("film %s has %d Kalshi strikes, more than one session's %d: its session will be refused",
                           film["film_id"], n, MAX_STRIKES)
        i = next((i for i, size in enumerate(sizes) if size + n <= MAX_STRIKES), None)
        if i is None:
            groups.append([])
            sizes.append(0)
            i = len(groups) - 1
        groups[i].append(film)
        sizes[i] += n
    return groups


def maker_triggers(films: pd.DataFrame, now: pd.Timestamp) -> list[tuple[str, dict]]:
    """("launch" | "shutdown", trigger data) for the rt_maker rule: a launch per group, and in RELAUNCH_HOURS a
    shutdown carrying every group (none at all once no film is open)."""
    groups = maker_groups(films, now)
    out = [("launch", {"group": i, "films": group}) for i, group in enumerate(groups)]
    if now.hour in RELAUNCH_HOURS:
        out.append(("shutdown", {"groups": groups}))
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
    newest = latest(films, "film_id") if len(films) else films
    found = [(RULE_TYPE, a, d) for a, d in triggers(newest, latest(reviews, "url") if len(reviews) else reviews, now,
                                                    scores)] if len(films) else []
    found += [(MAKER_RULE_TYPE, a, d) for a, d in maker_triggers(newest, now)] if len(films) else []
    queues = {"launch": os.environ["LAUNCHER_QUEUE_URL"], "shutdown": os.environ["SHUTDOWN_QUEUE_URL"]}
    sqs = boto3.client("sqs")
    for rule_type, action, data in found:
        send_trigger(sqs, queues[action], rule_type, action, data, now.isoformat(), None)
    counts = {f"{r} {a}": sum(1 for rt, x, _ in found if (rt, x) == (r, a))
              for r in (RULE_TYPE, MAKER_RULE_TYPE) for a in ("launch", "shutdown")}
    logger.info("rt scan: %d films, %s", len(films), counts)
    return counts
