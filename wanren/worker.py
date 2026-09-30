from __future__ import annotations

import time

from .config import load_settings
from .database import Database
from .service import WanRenService


def main() -> None:
    settings = load_settings()
    service = WanRenService(Database(settings.db_path), settings)
    worker_id = f"standalone-worker-{int(time.time())}"
    try:
        while True:
            results = service.run_pending_workflows(worker_id, limit=10)
            if not results:
                time.sleep(settings.worker_poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        service.db.close()


if __name__ == "__main__":
    main()
