from __future__ import annotations

import argparse
from pathlib import Path

from app.processor import process_csv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--out", default="/tmp/csv-fast-view-output")
    parser.add_argument("--format", choices=["xlsx", "sqlite", "parquet"], default="xlsx")
    args = parser.parse_args()

    def progress(rows: int, message: str | None = None, *args, **kwargs) -> None:
        if message:
            print(message)
        elif rows % 100000 == 0:
            print(f"processed {rows:,} rows")

    archive = process_csv(Path(args.csv_path), Path(args.out), args.format, progress)
    print(archive)
    print(archive.stat().st_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
