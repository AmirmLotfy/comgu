#!/usr/bin/env python3
"""Fix the MAE consumer stall in the DataHub quickstart.

Quickstart ships ES_BULK_REFRESH_POLICY=WAIT_UNTIL, which blocks every bulk
write until OpenSearch's next refresh (~1-3s) no matter how small the batch.
Under any real ingestion volume the consumer's batch processing then exceeds
max.poll.interval.ms, Kafka evicts it, it rebalances, replays the same batch,
and never makes progress -- which is exactly the stall we see.

Set the refresh policy to NONE and cap max.poll.records so a batch always
finishes well inside the poll interval.
"""

import re
import shutil
import sys
from pathlib import Path

COMPOSE = Path.home() / ".datahub/quickstart/docker-compose.yml"

WANT = {
    "ES_BULK_REFRESH_POLICY": "NONE",
    "SPRING_KAFKA_PROPERTIES_MAX_POLL_RECORDS": "50",
    "SPRING_KAFKA_PROPERTIES_MAX_POLL_INTERVAL_MS": "900000",
}


def main() -> int:
    if not COMPOSE.exists():
        print(f"MISSING {COMPOSE}")
        return 1

    shutil.copy(COMPOSE, COMPOSE.with_suffix(".yml.bak"))
    text = COMPOSE.read_text()
    lines = text.splitlines()

    # Find the gms service's environment block by locating the existing
    # ES_BULK_REFRESH_POLICY entry and matching its indentation.
    idx = next(
        (i for i, l in enumerate(lines) if "ES_BULK_REFRESH_POLICY" in l), None
    )
    if idx is None:
        print("could not find ES_BULK_REFRESH_POLICY in the compose file")
        return 1

    line = lines[idx]
    indent = re.match(r"\s*", line).group(0)
    dash = line.lstrip().startswith("-")

    def fmt(k: str, v: str) -> str:
        return f"{indent}- {k}={v}" if dash else f"{indent}{k}: {v}"

    lines[idx] = fmt("ES_BULK_REFRESH_POLICY", WANT["ES_BULK_REFRESH_POLICY"])
    print(f"set ES_BULK_REFRESH_POLICY=NONE (was WAIT_UNTIL) at line {idx + 1}")

    for key in ("SPRING_KAFKA_PROPERTIES_MAX_POLL_RECORDS",
                "SPRING_KAFKA_PROPERTIES_MAX_POLL_INTERVAL_MS"):
        if any(key in l for l in lines):
            j = next(i for i, l in enumerate(lines) if key in l)
            lines[j] = fmt(key, WANT[key])
            print(f"updated {key}={WANT[key]}")
        else:
            lines.insert(idx + 1, fmt(key, WANT[key]))
            print(f"added {key}={WANT[key]}")

    COMPOSE.write_text("\n".join(lines) + "\n")
    print(f"wrote {COMPOSE} (backup at {COMPOSE.with_suffix('.yml.bak')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
