"""Command-line interface. All user-facing text is English."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from artifacts import relative_to_root
from config import HEADLESS, INPUT_XLSX, OUTPUT_XLSX, ROOT, SUPPORTED_CARRIERS
from excel_io import ExcelReadError, build_output_frame, read_input, write_output
from runner import configure_logging, print_progress, print_summary, run_batch, run_single
from validate import normalize_carrier, normalize_container


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Track ocean containers from public carrier pages.",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Path to input Excel (default: input/containers.xlsx when used as batch).",
    )
    parser.add_argument("--container", help="Single container number.")
    parser.add_argument(
        "--carrier",
        help=f"Carrier code: {', '.join(SUPPORTED_CARRIERS)}",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser (debug only). Default is headless.",
    )
    parser.add_argument(
        "--wait-challenge",
        action="store_true",
        help=(
            "After automatic waits fail, keep a visible browser so you can "
            "complete Cloudflare/CAPTCHA. This is already the default."
        ),
    )
    parser.add_argument(
        "--no-wait-challenge",
        action="store_true",
        help=(
            "Do not wait for a human after automatic Cloudflare/CAPTCHA waits fail. "
            "Mark the row failed and pause that carrier if it repeats."
        ),
    )
    parser.add_argument("--limit", type=int, default=None, help="Track at most N rows.")
    parser.add_argument(
        "--carriers",
        help="Comma-separated carrier filter, e.g. HLCU,YMJA",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse SAILED rows from the existing result file.",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--dedupe",
        action="store_true",
        help="Keep the first row per Container+Carrier.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    headed = bool(args.headed or args.wait_challenge or not HEADLESS)
    wait_for_challenge = not bool(args.no_wait_challenge)

    print("Container Tracker")
    print("===================================")
    print()

    if args.container or args.carrier:
        if not args.container or not args.carrier:
            print("Both --container and --carrier are required for single-container mode.")
            return 2
        container = normalize_container(args.container)
        carrier = normalize_carrier(args.carrier)
        result = asyncio.run(
            run_single(
                carrier,
                container,
                headed=headed,
                wait_for_challenge=wait_for_challenge,
            )
        )
        print_progress(1, 1, result)
        print_summary([result], None)
        if args.output:
            row = {
                "Container": container,
                "Carrier": carrier,
                "extras": {},
            }
            output_path = args.output if args.output.is_absolute() else (ROOT / args.output)
            path = write_output(output_path, build_output_frame([row], [result]))
            print(f"Output: {relative_to_root(path) or path}")
        return 0 if result.success or result.status == "MANUAL_CHECK_REQUIRED" else 1

    input_path = Path(args.input) if args.input else INPUT_XLSX
    if not input_path.is_absolute():
        input_path = (ROOT / input_path).resolve()
    try:
        rows = read_input(input_path, dedupe=args.dedupe)
    except ExcelReadError as exc:
        print(str(exc))
        return 2
    if args.carriers:
        allow = {c.strip().upper() for c in args.carriers.split(",") if c.strip()}
        rows = [row for row in rows if row["Carrier"] in allow]
    if args.limit is not None:
        rows = rows[: args.limit]
    if not rows:
        print("No container rows to track.")
        return 2

    output_path = Path(args.output) if args.output else OUTPUT_XLSX
    if not output_path.is_absolute():
        output_path = (ROOT / output_path).resolve()

    results, written = asyncio.run(
        run_batch(
            rows,
            headed=headed,
            wait_for_challenge=wait_for_challenge,
            resume=args.resume,
            output_path=output_path,
        )
    )
    print_summary(results, written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
