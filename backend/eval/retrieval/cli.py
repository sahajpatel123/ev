"""ev-eval CLI: retrieval comparison + re-embed job (Agent 8 / SYNAPSE).

Runnable today as ``python -m eval.retrieval.cli`` from backend/; the
``ev-eval`` console script is a DEP REQUEST for Agent 2 (pyproject).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ev-eval")
    sub = parser.add_subparsers(dest="command", required=True)

    p_retrieval = sub.add_parser(
        "retrieval",
        help="run the retrieval eval (synthetic corpus by default; live DB with --questions)",
    )
    p_retrieval.add_argument(
        "--questions",
        type=Path,
        help="JSON file with {questions: [{query, expected_memory_ids}]}; "
        "requires --database-url for real memory ids",
    )
    p_retrieval.add_argument("--out", type=Path, help="write the JSON report here")
    p_retrieval.add_argument("--k", type=int, default=10)
    p_retrieval.add_argument("--rerank", action="store_true", help="enable the reranker pass")
    p_retrieval.add_argument(
        "--database-url",
        help="run against an existing database instead of a seeded synthetic corpus",
    )
    p_retrieval.add_argument(
        "--dataset",
        choices=("auto", "synthetic", "personal"),
        default="auto",
        help="label written into the report; auto = personal when --questions "
        "is given, synthetic otherwise",
    )
    p_retrieval.set_defaults(func=_cmd_retrieval)

    p_reembed = sub.add_parser("reembed", help="resumable re-embed of all current memories")
    p_reembed.add_argument("--batch-size", type=int, default=32)
    p_reembed.add_argument("--max-rows", type=int, default=None)
    p_reembed.add_argument("--allow-degraded", action="store_true")
    p_reembed.add_argument(
        "--database-url",
        default=os.environ.get("EV_DATABASE_URL"),
        help="defaults to EV_DATABASE_URL",
    )
    p_reembed.set_defaults(func=_cmd_reembed)

    return parser


def _cmd_retrieval(args: argparse.Namespace) -> int:
    async def run() -> dict:
        from eval.retrieval.harness import run_comparison
        from eval.retrieval.synthetic_corpus import build_synthetic_corpus

        questions = None
        corpus = None
        dataset = args.dataset
        if args.questions:
            from eval.retrieval.harness import _load_questions

            questions = _load_questions(args.questions)
            if dataset == "auto":
                dataset = "personal"
        else:
            corpus = build_synthetic_corpus()
            if dataset == "auto":
                dataset = "synthetic"
        return await run_comparison(
            corpus,
            questions=questions,
            k=args.k,
            rerank=args.rerank,
            database_url=args.database_url,
            dataset=dataset,
        )

    report = asyncio.run(run())
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"report written to {args.out}")
    else:
        print(json.dumps(report, indent=2, default=str))
    return 0


def _cmd_reembed(args: argparse.Namespace) -> int:
    if not args.database_url:
        print("--database-url or EV_DATABASE_URL is required", file=sys.stderr)
        return 2

    async def run() -> dict:
        from sqlalchemy.ext.asyncio import (
            async_sessionmaker,
            create_async_engine,
        )

        from app.embeddings import reembed_all_memories

        def progress(info: dict) -> None:
            print(
                f"\rre-embed {info['done']}/{info['total']} "
                f"({info['embedded']} new, {info['skipped']} already current)",
                end="",
                flush=True,
            )

        engine = create_async_engine(args.database_url)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            report = await reembed_all_memories(
                session,
                batch_size=args.batch_size,
                max_rows=args.max_rows,
                require_real=not args.allow_degraded,
                on_progress=progress,
            )
        await engine.dispose()
        print()
        return report.to_dict()

    try:
        report = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - CLI reports and exits non-zero
        print(f"reembed failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
