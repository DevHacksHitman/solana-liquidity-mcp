"""Generate the bundled demo event fixture (synthetic, deterministic).

Identifiers are deliberately non-canonical (``sig-demo-0001``, ``MINT-DEMO-A``)
so the repository never contains key-shaped or address-shaped literals — the
same hygiene rule the tracker's fixtures follow. The timeline contains a
deliberate ~95-second quiet stretch so the snapshot tool's gap detection has
something real to report.

    python3 scripts/make_demo_events.py > data/demo_events.jsonl
"""

import json
import random
import sys
from datetime import datetime, timedelta, timezone

SEED = 20261028
START = datetime(2026, 10, 28, 18, 0, 0, tzinfo=timezone.utc)
COUNT = 84
GAP_AT = 40          # index where the quiet stretch begins
GAP_SECONDS = 95
MINTS = ["MINT-DEMO-A", "MINT-DEMO-B", "MINT-DEMO-C", "MINT-DEMO-D"]


def main() -> int:
    rng = random.Random(SEED)
    offset = 0
    for index in range(COUNT):
        if index == GAP_AT:
            offset += GAP_SECONDS
        else:
            offset += rng.randint(1, 5)
        mint = None if index % 9 == 8 else rng.choice(MINTS)
        event = {
            "signature": f"sig-demo-{index + 1:04d}",
            "slot": 300000000 + index * 3 + rng.randint(0, 2),
            "side": "buy" if rng.random() < 0.62 else "sell",
            "mint": mint,
            "trader": None if index % 7 == 3 else f"TRADER-DEMO-{rng.randint(1, 12):02d}",
            "token_amount": None if index % 5 == 0 else round(rng.uniform(500, 90000), 2),
            "sol_amount": round(rng.uniform(0.05, 12.0), 4),
            "observed_at": (START + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        print(json.dumps(event, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
