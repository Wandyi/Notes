"""CLI entry point for the fan-out research agent.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python main.py "What are the tradeoffs of DNS-over-HTTPS for enterprise networks?"
    python main.py --max-concurrency 6 --researcher-model claude-sonnet-5 "..."
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from fanout_research import Orchestrator, Settings


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fan-out research agent (Claude API).")
    p.add_argument("question", help="The broad research question to investigate.")
    p.add_argument("--max-concurrency", type=int, default=Settings.max_concurrency,
                   help="Max concurrent researcher subagents.")
    p.add_argument("--max-subquestions", type=int, default=Settings.max_subquestions)
    p.add_argument("--planner-model", default=Settings.planner_model)
    p.add_argument("--researcher-model", default=Settings.researcher_model)
    p.add_argument("--synthesizer-model", default=Settings.synthesizer_model)
    p.add_argument("--quiet", action="store_true", help="Suppress progress + trace output.")
    return p.parse_args(argv)


async def _main(argv: list[str]) -> int:
    args = _parse_args(argv)
    settings = Settings(
        planner_model=args.planner_model,
        researcher_model=args.researcher_model,
        synthesizer_model=args.synthesizer_model,
        max_concurrency=args.max_concurrency,
        max_subquestions=args.max_subquestions,
    )
    orchestrator = Orchestrator(settings=settings)
    report = await orchestrator.run(args.question, verbose=not args.quiet)

    print("═" * 78)
    print("REPORT")
    print("═" * 78)
    print(report)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main(sys.argv[1:])))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
