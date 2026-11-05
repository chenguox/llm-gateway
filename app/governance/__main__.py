"""Read private call records locally without publishing an unauthenticated API."""

import argparse
import asyncio
import json
from datetime import UTC, datetime

from app.core.config import Settings
from app.governance.store import RecordStorageError, RecordStore


def since_timestamp(value: str) -> str:
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp.astimezone(UTC).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("Use an ISO timestamp with a timezone") from None


def main() -> None:
    parser = argparse.ArgumentParser(description="Query local LLM call accounting records")
    parser.add_argument("--request-id")
    parser.add_argument("--model")
    parser.add_argument("--since", type=since_timestamp)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--call-id", help="List attempts belonging to this call")
    arguments = parser.parse_args()
    settings = Settings()
    if not settings.call_records_path.is_file():
        parser.exit(1, "Call record database not found\n")
    store = RecordStore(settings.call_records_path)

    async def query() -> list[dict]:
        if arguments.call_id is not None:
            return await store.find_attempts(arguments.call_id)
        return await store.find_calls(
            request_id=arguments.request_id,
            model=arguments.model,
            since=arguments.since,
            limit=arguments.limit,
        )

    try:
        print(json.dumps(asyncio.run(query()), ensure_ascii=False, indent=2))
    except RecordStorageError:
        parser.exit(1, "Call record storage unavailable\n")


if __name__ == "__main__":
    main()
