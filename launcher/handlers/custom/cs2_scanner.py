"""
Each time a cs2_upcoming run succeeds: turn gnome-pipelines' cs2_upcoming_matches into cs2 launch and
shutdown triggers. gnome-pipelines' runner announces the run on the default EventBridge bus; the event
itself carries nothing this needs, since the table holds every match's newest state.

cs2_upcoming tracks a BO3 from 12 hours before its HLTV kickoff and writes a row per match each run:
its HLTV status (upcoming, live, then missing once it leaves HLTV's listing) and, once a venue's
series market is in the registry, a leg per venue and market with the two team listings. For the
newest row of each match:

- launch, per leg, while HLTV still lists it upcoming, kickoff is under LAUNCH_LEAD away and the row
  is fresh (a stalled pipeline would otherwise keep launching sessions onto a frozen feed);
- shut down, per leg, once HLTV lists it missing (it has left the listing: finished, or dropped), or
  SHUTDOWN_AFTER past kickoff. A live match is left running: the session stops trading at kickoff
  but keeps marking its positions until the match ends.

Each scan resends the same triggers; the launcher's dedup and its active-session check make the
repeats no-ops, and a session that died before kickoff is relaunched by a later scan. A launch trigger's
deadline is the kickoff, so a request still awaiting approval then can't launch; a moved kickoff moves it.
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
MISSING = "missing"
# Matches are tracked from 12h out; the hour of slack covers a run that lands just before the window.
LAUNCH_LEAD = pd.Timedelta(hours=13)
# The strategy exits on its own once the match ends, or 8h after kickoff if the pipeline stalls; this frees
# the instance if it doesn't.
SHUTDOWN_AFTER = pd.Timedelta(hours=9)
# cs2_upcoming runs every 15 minutes; a newest row older than this means it had stalled before this run.
FRESH = pd.Timedelta(hours=1)
# Rows newer than this cover every match still worth a trigger: tracked matches get a row every run.
LOOKBACK = pd.Timedelta(hours=36)


def latest(rows: pd.DataFrame) -> pd.DataFrame:
    return rows.sort_values("generated_at", kind="stable").drop_duplicates("match_id", keep="last")


def triggers(matches: pd.DataFrame, now: pd.Timestamp) -> list[tuple[str, dict, str | None]]:
    """("launch" | "shutdown", trigger data, deadline) for each leg of each match's newest row; a launch's deadline
    is the kickoff, a shutdown has none."""
    out = []
    for m in matches.itertuples():
        kickoff = pd.Timestamp(m.kickoff)
        if m.status == UPCOMING and now < kickoff <= now + LAUNCH_LEAD:
            if now - pd.Timestamp(m.generated_at) > FRESH:
                continue
            action = "launch"
        elif m.status == MISSING or now >= kickoff + SHUTDOWN_AFTER:
            action = "shutdown"
        else:
            continue
        for leg in json.loads(m.markets or "[]"):
            out.append((action, {
                "match_id": int(m.match_id), "venue": leg["venue"], "market": leg["market"],
                "listing_team_a": int(leg["listing_team_a"]), "listing_team_b": int(leg["listing_team_b"]),
            }, kickoff.isoformat() if action == "launch" else None))
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
