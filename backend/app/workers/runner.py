"""RQ worker entrypoint (compose worker service)."""

import asyncio

from redis import Redis
from rq import Worker

from app.config import settings
from app.db import init_db


def main() -> None:
    # The API creates the schema in its lifespan, but this worker can start
    # first; init_db is idempotent and also creates pgvector's extension.
    asyncio.run(init_db())
    redis = Redis.from_url(settings.redis_url)
    worker = Worker(["ingestion"], connection=redis)
    worker.work()


if __name__ == "__main__":
    main()
