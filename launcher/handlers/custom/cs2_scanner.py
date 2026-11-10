"""
Each time a cs2_upcoming run succeeds: turn gnome-pipelines' cs2_upcoming_matches into cs2 launch and
shutdown triggers. gnome-pipelines' runner announces the run on the default EventBridge bus; the event
itself carries nothing this needs, since the table holds every match's newest state.

cs2_upcoming writes a row per tracked BO3 each run: its HLTV status (upcoming, live, then missing once
it leaves HLTV's listing, or finished once a venue has paid out its series market) and, once a venue's market is in the registry, a leg per venue and market with
the two team listings. For the newest row of each match, one trigger per market holding every venue's
leg, since one session trades the match wherever it is listed:

- launch while HLTV still lists it upcoming, kickoff is under LAUNCH_LEAD away but more than
  LAUNCH_CUTOFF away, and the row is fresh (a stalled pipeline would otherwise keep launching sessions
  onto a frozen feed);
- launch with phase "live" while HLTV lists it live and the row is fresh, for rules whose strategy trades
  the match while it is played: a match it missed before kickoff (the pipeline was down, or HLTV listed it
  late) is still picked up, and a session that died mid-match is restarted;
- shut down once it is finished, once HLTV lists it missing (it has left the listing: finished, or dropped),
  or SHUTDOWN_AFTER past kickoff. cs2_series ends itself at kickoff, so for it these only catch a session
  that didn't. The trigger says which (`reason`): a rule whose strategy trades the match while it is played
  ignores PAST_KICKOFF and stops on FINISHED or GONE.

Each scan resends the same triggers; the launcher's dedup and its active-session check make the
repeats no-ops, and a session that died before the cutoff is relaunched by a later scan. Legs are sorted
by venue so the same legs are always the same trigger. A venue listed after the session launched makes a
new trigger, which the active-session check blocks: the running session holds the match's other listings.
A launch trigger's deadline is the cutoff, so a request still awaiting approval then can't launch; a moved
kickoff moves it. Past the cutoff nothing launches, which is also what keeps a session that ended there
from being started again.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import boto3
import pandas as pd
from gnomepy.data import DatasetStore
from pyiceberg.expressions import GreaterThanOrEqual

from launcher.queues import send_trigger

logger = logging.getLogger(__name__)

MATCHES = "cs2_upcoming_matches"
RULE_TYPE = "cs2"
UPCOMING = "upcoming"
LIVE = "live"
MISSING = "missing"
FINISHED = "finished"
# cs2_series trades the last hour before kickoff; launching an hour earlier leaves time for the instance to boot
# and a missed scan or two.
LAUNCH_LEAD = pd.Timedelta(hours=2)
# The instance takes minutes to boot, and cs2_series ends itself at kickoff; a session launched later could
# hardly trade.
LAUNCH_CUTOFF = pd.Timedelta(minutes=10)
# The strategy exits on its own at kickoff, even if the pipeline stalls; this frees the instance if it doesn't.
SHUTDOWN_AFTER = pd.Timedelta(hours=1)
# cs2_upcoming runs every 15 minutes; a newest row older than this means it had stalled before this run.
FRESH = pd.Timedelta(hours=1)
FINISHED_REASON, GONE, PAST_KICKOFF = "finished", "gone", "past_kickoff"
# Rows newer than this cover every match still worth a trigger: tracked matches get a row every run.
LOOKBACK = pd.Timedelta(hours=36)


def latest(rows: pd.DataFrame) -> pd.DataFrame:
    return rows.sort_values("generated_at", kind="stable").drop_duplicates("match_id", keep="last")


def triggers(matches: pd.DataFrame, now: pd.Timestamp) -> list[tuple[str, dict, str | None]]:
    """("launch" | "shutdown", trigger data, deadline) for each market of each match's newest row, with every venue's
    leg; a launch's deadline is the cutoff, a shutdown has none."""
    out = []
    for m in matches.itertuples():
        kickoff = pd.Timestamp(m.kickoff)
        fresh = now - pd.Timestamp(m.generated_at) <= FRESH
        actions: list[tuple[str, dict, str | None]] = []
        if m.status == UPCOMING and now + LAUNCH_CUTOFF < kickoff <= now + LAUNCH_LEAD and fresh:
            actions.append(("launch", {}, (kickoff - LAUNCH_CUTOFF).isoformat()))
        if m.status == LIVE and fresh:
            actions.append(("launch", {"phase": "live"}, None))
        if m.status in (FINISHED, MISSING) or now >= kickoff + SHUTDOWN_AFTER:
            reason = {FINISHED: FINISHED_REASON, MISSING: GONE}.get(m.status, PAST_KICKOFF)
            actions.append(("shutdown", {"reason": reason}, None))
        if not actions:
            continue
        by_market: dict[str, list[dict]] = {}
        for leg in json.loads(m.markets or "[]"):
            by_market.setdefault(leg["market"], []).append({
                "venue": leg["venue"], "listing_team_a": int(leg["listing_team_a"]),
                "listing_team_b": int(leg["listing_team_b"])})
        for market, legs in by_market.items():
            data = {"match_id": int(m.match_id), "market": market, "legs": sorted(legs, key=lambda leg: leg["venue"])}
            out += [(action, {**data, **extra}, deadline) for action, extra, deadline in actions]
    return out


def handler(event, context):
    now = pd.Timestamp(datetime.now(timezone.utc))
    rows = DatasetStore().load(MATCHES, columns=["match_id", "kickoff", "status", "markets", "generated_at"],
                               row_filter=GreaterThanOrEqual("generated_at", (now - LOOKBACK).isoformat()))
    queues = {"launch": os.environ["LAUNCHER_QUEUE_URL"], "shutdown": os.environ["SHUTDOWN_QUEUE_URL"]}
    found = triggers(latest(rows), now) if len(rows) else []
    sqs = boto3.client("sqs")
    for action, data, deadline in found:
        send_trigger(sqs, queues[action], RULE_TYPE, action, data, now.isoformat(), deadline)
    counts = {a: sum(1 for x, _, _ in found if x == a) for a in ("launch", "shutdown")}
    logger.info("cs2 scan: %d matches, %s", rows.match_id.nunique() if len(rows) else 0, counts)
    return counts
