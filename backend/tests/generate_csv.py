from __future__ import annotations

import argparse
from pathlib import Path


def generate_csv(path: Path, target_mb: int) -> None:
    target_bytes = target_mb * 1024 * 1024
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "Timestamp,CHW Supply Temp,CHW Return Temp,CHWP Status,Alarm Status,CHWP Speed Hz,DP Pressure,Flow LPS\n"
    row_index = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(header)
        while handle.tell() < target_bytes:
            minute = row_index % 60
            hour = (row_index // 60) % 24
            day = 1 + (row_index // 1440) % 28
            handle.write(
                f"2026-06-{day:02d} {hour:02d}:{minute:02d}:00,"
                f"{6.5 + (row_index % 25) / 10:.2f},"
                f"{11.0 + (row_index % 40) / 10:.2f},"
                f"Run,"
                f"{'Alarm' if row_index % 997 == 0 else 'Normal'},"
                f"{38 + (row_index % 20) / 2:.2f},"
                f"{120 + (row_index % 70) / 3:.2f},"
                f"{18 + (row_index % 30) / 2:.2f}\n"
            )
            row_index += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--mb", type=int, default=400)
    args = parser.parse_args()
    generate_csv(Path(args.path), args.mb)
    print(Path(args.path).stat().st_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
