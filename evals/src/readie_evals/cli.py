"""The ``readie-evals`` command line.

Three phases, each resumable and safe to re-invoke: ``fetch`` builds the corpus,
``run`` executes and measures it, ``report`` summarizes a run. ``all`` chains them.
``sample`` is a fourth, standalone entry point: it ignores the corpus's existing
counts and always adds a fresh batch, spread evenly across randomly ordered
(source, category) pairs.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import random
import socket
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from typing import Any

from readie_evals.agent.foundry_llm import FoundryLLM
from readie_evals.agent.ports import LLM, Fetcher
from readie_evals.agent.repair import repair_cell
from readie_evals.build import build_corpus, sample_corpus
from readie_evals.catalogue import warm_imports
from readie_evals.config import ConfigError, Settings
from readie_evals.corpus import CorpusStore
from readie_evals.execution import Executor, Target
from readie_evals.models import Task
from readie_evals.report import write_reports
from readie_evals.results import ResultsStore
from readie_evals.runner import Repairer, Runner
from readie_evals.taxonomy import ALL_SOURCES, CATEGORIES, SOURCES


def _default_run_id(corpus: list[Task]) -> str:
    if not corpus:
        return "empty"
    digest = hashlib.sha256("".join(sorted(task.id for task in corpus)).encode())
    return digest.hexdigest()[:12]


def _fetchers(sources: Sequence[str]) -> dict[str, Fetcher]:
    """Construct the fetchers a source list needs (clients load lazily on use).

    ``kaggle-notebook``/``huggingface-notebook`` reuse the same underlying client as
    ``kaggle``/``huggingface`` -- they differ only in how the harness adapts what
    comes back, not in how it's fetched.
    """
    built: dict[str, Fetcher] = {}
    if {"kaggle", "kaggle-notebook"} & set(sources):
        from readie_evals.fetch.kaggle import KaggleFetcher  # noqa: PLC0415

        built["kaggle"] = KaggleFetcher()
    if {"huggingface", "huggingface-notebook"} & set(sources):
        from readie_evals.fetch.huggingface import HuggingFaceFetcher  # noqa: PLC0415

        built["huggingface"] = HuggingFaceFetcher()
    return built


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    overrides: dict[str, Any] = {}
    if getattr(args, "model", None):
        overrides["azure_model"] = args.model
    if getattr(args, "router_uri", None):
        overrides["router_uri"] = args.router_uri
    if getattr(args, "per_cell", None):
        overrides["per_cell"] = args.per_cell
    if getattr(args, "tls", False):
        overrides["tls"] = True
    if getattr(args, "concurrency", None):
        overrides["concurrency"] = args.concurrency
    if getattr(args, "release_wait", None) is not None:
        overrides["container_release_wait"] = args.release_wait
    return dataclasses.replace(settings, **overrides) if overrides else settings


def _cmd_fetch(args: argparse.Namespace) -> int:
    settings = _settings(args)
    try:
        llm: LLM = FoundryLLM.from_settings(settings)
    except ConfigError as exc:
        print(f"error: {exc}")
        return 2
    sources = args.sources or list(SOURCES)
    categories = args.categories or list(CATEGORIES)
    corpus = CorpusStore(settings.corpus_path)
    build_corpus(settings, llm, _fetchers(sources), corpus, sources=sources, categories=categories)
    return 0


def _cmd_sample(args: argparse.Namespace) -> int:
    settings = _settings(args)
    try:
        llm: LLM = FoundryLLM.from_settings(settings)
    except ConfigError as exc:
        print(f"error: {exc}")
        return 2
    sources = args.sources or list(ALL_SOURCES)
    categories = args.categories or list(CATEGORIES)
    corpus = CorpusStore(settings.corpus_path)
    rng = random.Random(args.seed) if args.seed is not None else random.Random()  # noqa: S311
    sample_corpus(
        settings,
        llm,
        _fetchers(sources),
        corpus,
        sources=sources,
        categories=categories,
        count=args.count,
        rng=rng,
    )
    return 0


def _router_reachable(uri: str, *, timeout: float = 3.0) -> bool:
    """Whether a TCP connection to ``host:port`` succeeds, fast, with no gRPC call."""
    host, _, port = uri.partition(":")
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _cmd_run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    corpus = CorpusStore(settings.corpus_path).load()
    if not corpus:
        print("no corpus yet; run `readie-evals fetch` first")
        return 1
    target = Target(args.target)
    if target is Target.REAL and not _router_reachable(settings.router_uri):
        print(
            f"error: readie router not reachable at {settings.router_uri}; "
            "is the stack up? (`make run-local`, or `run --target local`)",
        )
        return 2
    run_id = args.run_id or _default_run_id(corpus)
    results = ResultsStore(settings.results_path)
    concurrency = settings.concurrency if target is Target.REAL else 1
    # Two pools, deliberately: this one bounds concurrent containers (shared by
    # every task's checkpoint/cold-start sides); Runner gets its own, separate
    # one for concurrent tasks. Nesting one pool inside itself would deadlock
    # once every worker were a task blocked on its own container jobs. See
    # Executor's and Runner's docstrings.
    container_pool = ThreadPoolExecutor(max_workers=concurrency) if concurrency > 1 else None
    try:
        with container_pool or nullcontext():
            runner = Runner(
                Executor(settings, target=target, pool=container_pool),
                results,
                target=target,
                repair=_repair(settings),
                concurrency=concurrency,
            )
            print(
                f"[*] run {run_id} ({target.value}, {len(corpus)} task(s), "
                f"concurrency {concurrency}, release wait {settings.container_release_wait:g}s)"
            )
            stats = runner.run(corpus, run_id=run_id, limit=args.limit)
    finally:
        results.close()
    print(
        f"[=] ok {stats.ok}, repaired {stats.repaired}, "
        f"failed {stats.failed}, skipped {stats.skipped}",
    )
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    settings = _settings(args)
    results = ResultsStore(settings.results_path)
    try:
        run_id = args.run_id or (results.run_ids()[0] if results.run_ids() else None)
        if run_id is None:
            print("no results yet; run `readie-evals run` first")
            return 1
        csv_path, md_path = write_reports(results, run_id, settings.reports_dir)
    finally:
        results.close()
    print(md_path.read_text())
    print(f"[=] wrote {md_path} and {csv_path}")
    return 0


def _cmd_all(args: argparse.Namespace) -> int:
    code = _cmd_fetch(args)
    if code != 0:
        return code
    code = _cmd_run(args)
    if code != 0:
        return code
    return _cmd_report(args)


def _repair(settings: Settings) -> Repairer | None:
    """A one-shot, same-session repairer wired to the agent, or ``None`` if unconfigured."""
    if not settings.agent_configured:
        print("note: Foundry not configured (AZURE_*); failures will not be repaired")
        return None
    llm = FoundryLLM.from_settings(settings)
    warm = warm_imports(settings.catalogue_path)

    def repair(
        category: str, code: str, error_type: str, error_message: str
    ) -> tuple[str, tuple[str, ...]] | None:
        return repair_cell(
            llm,
            category=category,
            code=code,
            error_type=error_type,
            error_message=error_message,
            warm=warm,
        )

    return repair


def _add_fetch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sources",
        nargs="*",
        choices=ALL_SOURCES,
        help="default: kaggle, huggingface, generated (session sources are opt-in)",
    )
    parser.add_argument("--categories", nargs="*", choices=CATEGORIES, help="default: all")
    parser.add_argument("--per-cell", type=int, help="tasks per (source, category)")


def _add_sample_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--count", type=int, default=10, help="how many tasks to sample")
    parser.add_argument(
        "--sources",
        nargs="*",
        choices=ALL_SOURCES,
        help="default: every source, including the session ones",
    )
    parser.add_argument("--categories", nargs="*", choices=CATEGORIES, help="default: all")
    parser.add_argument("--seed", type=int, help="seed the random pick, for a repeatable sample")


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", choices=[t.value for t in Target], default=Target.REAL.value)
    parser.add_argument("--router-uri", help="override the router host:port")
    parser.add_argument("--tls", action="store_true", help="connect over TLS")
    parser.add_argument("--run-id", help="group results under this id")
    parser.add_argument("--limit", type=int, help="run at most this many tasks")
    parser.add_argument(
        "--concurrency", type=int, help="max containers in flight at once (default: 10)"
    )
    parser.add_argument(
        "--release-wait",
        type=float,
        help="seconds to hold a container slot after it finishes (default: 35)",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch to a subcommand."""
    parser = argparse.ArgumentParser(prog="readie-evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="fetch and adapt tasks into the corpus")
    _add_fetch_args(fetch)
    fetch.add_argument("--model", help="override the agent model id")
    fetch.set_defaults(func=_cmd_fetch)

    sample = sub.add_parser(
        "sample",
        help="add a fresh batch, spread evenly across random (source, category) pairs",
    )
    _add_sample_args(sample)
    sample.add_argument("--model", help="override the agent model id")
    sample.set_defaults(func=_cmd_sample)

    run = sub.add_parser("run", help="execute and measure the corpus")
    _add_run_args(run)
    run.add_argument("--model", help="override the repair model id")
    run.set_defaults(func=_cmd_run)

    report = sub.add_parser("report", help="summarize a run")
    report.add_argument("--run-id", help="the run to summarize (default: the largest)")
    report.set_defaults(func=_cmd_report)

    everything = sub.add_parser("all", help="fetch, run and report")
    _add_fetch_args(everything)
    _add_run_args(everything)
    everything.add_argument("--model", help="override the agent model id")
    everything.set_defaults(func=_cmd_all)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
