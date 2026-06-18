from __future__ import annotations

import argparse
from pathlib import Path

from app.processor import process_csv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--out", default="/tmp/csv-fast-view-output")
    args = parser.parse_args()

    def progress(rows: int, message: str | None = None, *args, **kwargs) -> None:
        if message:
            print(message)
        elif rows % 100000 == 0:
            print(f"processed {rows:,} rows")

    output_path = Path(args.out) / f"{Path(args.csv_path).stem}.xlsx"
    output = process_csv(Path(args.csv_path), output_path, "xlsx", progress)
    print(output)
    print(output.stat().st_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
