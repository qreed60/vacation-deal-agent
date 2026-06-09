from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.free_travel_probe import CANDIDATES, ProbeRequest, run_probe


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        description="Probe free/open-source travel price source candidates without enabling production search sources."
    )
    group = command_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--candidate", choices=CANDIDATES, help="Candidate source to probe")
    group.add_argument("--all", action="store_true", help="Probe every known candidate")
    command_parser.add_argument("--origin", help="Flight origin airport/city code")
    command_parser.add_argument("--destination", help="Flight destination code or hotel destination")
    command_parser.add_argument("--depart", help="Flight departure date, YYYY-MM-DD")
    command_parser.add_argument("--return", dest="return_date", help="Flight return date, YYYY-MM-DD")
    command_parser.add_argument("--check-in", dest="check_in", help="Hotel check-in date, YYYY-MM-DD")
    command_parser.add_argument("--check-out", dest="check_out", help="Hotel check-out date, YYYY-MM-DD")
    command_parser.add_argument("--adults", type=int, default=1, help="Adult traveler count")
    command_parser.add_argument("--children", type=int, default=0, help="Child traveler count")
    return command_parser


def _requests(args: argparse.Namespace) -> list[ProbeRequest]:
    candidates = CANDIDATES if args.all else (args.candidate,)
    return [
        ProbeRequest(
            candidate=candidate,
            origin=args.origin,
            destination=args.destination,
            depart=args.depart,
            return_date=args.return_date,
            check_in=args.check_in,
            check_out=args.check_out,
            adults=args.adults,
            children=args.children,
        )
        for candidate in candidates
    ]


def _print_summary(report: dict) -> None:
    print("Free travel source probe")
    print(f"Report: {report['report_path']}")
    print()
    for result in report["results"]:
        label = result.get("label") or result.get("component_type") or "unknown"
        print(f"- {result['candidate']} [{result['component_type']}]: {result['status']} ({label})")
        if result.get("provider") and result.get("total_price") is not None:
            print(
                f"  provider={result['provider']} "
                f"price={result['total_price']} {result.get('currency') or ''}".strip()
            )
        if result.get("install_hint"):
            print(f"  install_hint={result['install_hint']}")
        if result.get("error"):
            print(f"  error={result['error']}")
        notes = result.get("notes") or []
        for note in notes[:5]:
            print(f"  note={note}")
        if len(notes) > 5:
            print(f"  note=... {len(notes) - 5} more note(s) in report")


def main() -> int:
    args = parser().parse_args()
    report = run_probe(_requests(args))
    _print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
