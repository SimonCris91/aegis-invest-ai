"""Create a worker-only artifact; explicit files prevent packaging credentials."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main() -> None:
    source = Path(__file__).resolve().parent
    destination = Path("D:/Aegis/ComputeWorker/AEGIS-ComputeWorker.zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as bundle:
        for filename in ("__init__.py", "protocol.py", "coordinator.py", "worker.py"):
            bundle.write(source / filename, "aegis_compute/" + filename)
        bundle.write(source.parents[1] / "reports" / "distributed-compute-setup.md", "LEGGIMI.md")
    print(
        json.dumps(
            {
                "artifact": str(destination),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "contains_broker_credentials": False,
                "requires_python": "3.12+",
                "live_runner_integration": False,
            }
        )
    )


if __name__ == "__main__":
    main()
