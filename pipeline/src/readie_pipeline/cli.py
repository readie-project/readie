"""The pipeline's command line.

Four stages, each runnable alone::

    readie-pipeline corpus    build the workload corpus for one flavor
    readie-pipeline analyze   measure package sizes and import times
    readie-pipeline plan      choose checkpoint contents, write the OCI spec
    readie-pipeline build     capture one gVisor checkpoint per planned set

plus ``capture``, which is ``plan`` and ``build`` in one process because the
image's entrypoint needs them to share a container. See :func:`cmd_capture`.

This replaces ``setup.py`` and ``main.py``, which were module-scope scripts:
they executed on *import*, so neither could be tested, and neither stage could
be run without the other.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import numpy as np

from readie_pipeline.capture.build import CaptureError, capture, measure_time
from readie_pipeline.capture.rootfs import discover_rootfs_inventory, record_skipped
from readie_pipeline.capture.spec import ExecutorMode, SpecError, build_config, runsc_version
from readie_pipeline.catalogue import build_catalogue, write_catalogue
from readie_pipeline.config import ALPHA_PRECISION, ConfigError, CorpusSettings, Settings
from readie_pipeline.corpus.generate import Report
from readie_pipeline.corpus.models import Corpus, CorpusError, ResourceKind, Source
from readie_pipeline.manifest import CheckpointMeta, Manifest, write_plan
from readie_pipeline.metadata.analyze import analyze, installed_packages
from readie_pipeline.metadata.datasets import measure_datasets
from readie_pipeline.metadata.models import Metadata, MetadataError
from readie_pipeline.planning.ports import Budget, CheckpointPlan, CheckpointPlanner
from readie_pipeline.planning.requirements import (
    distributions_for,
    requirement_lines,
    write_requirements,
)


def build_planner(name: str, alpha: float) -> CheckpointPlanner:
    """Resolve a planner by name.

    ``alpha`` is the shared size-vs-time weight; only the greedy planner uses it.
    """
    # Both nested imports avoid an import cycle.
    from readie_pipeline.planning.fixed import FixedPlanner  # noqa: PLC0415

    if name == "fixed":
        return FixedPlanner()
    if name == "greedy":
        from readie_pipeline.planning.greedy import GreedyPlanner  # noqa: PLC0415

        return GreedyPlanner(alpha=alpha)

    msg = f"unknown planner {name!r}; choose from fixed, greedy"
    raise ConfigError(msg)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------
def cmd_corpus(settings: Settings, args: argparse.Namespace) -> int:
    """Generate the corpus for this run's flavor, or re-verify an existing one."""
    from readie_pipeline.corpus.domains import DOMAINS  # noqa: PLC0415
    from readie_pipeline.corpus.mix import parse_weights, schedule  # noqa: PLC0415
    from readie_pipeline.corpus.models import Source  # noqa: PLC0415

    only_sources = tuple(Source(s) for s in args.source or ())
    only_domains = tuple(args.domain or ())
    corpus_path = args.output or settings.corpus_path

    if args.dry_run:
        env = os.environ
        weights = parse_weights((env.get("READIE_CORPUS_SOURCE_WEIGHTS") or "").strip())
        if only_sources:
            weights = {s: (w if s in only_sources else 0.0) for s, w in weights.items()}
        total = args.total or int(env.get("READIE_CORPUS_TOTAL") or 2000)
        seed = int(env["READIE_CORPUS_SEED"]) if env.get("READIE_CORPUS_SEED") else None
        plan = schedule(total, weights, only_domains or DOMAINS, seed)
        print(f"[*] {settings.flavor} corpus, adding {total} items -> {corpus_path}")
        for source in Source:
            share = sum(n for (_, src), n in plan.items() if src is source)
            print(f"    {source.value}: {share}")
        return 0

    if args.clean:
        return _clean(settings, args, corpus_path)

    corpus_settings = CorpusSettings.from_env()
    if args.no_rewrite:
        corpus_settings = replace(corpus_settings, rewrite=False)

    if args.revalidate:
        from readie_pipeline.corpus.generate import revalidate  # noqa: PLC0415 - optional extra
        from readie_pipeline.corpus.live import build_verifier  # noqa: PLC0415 - optional extra

        verifier = build_verifier(
            corpus_settings, settings.corpus_cache_path, _installed_packages(settings)
        )
        revalidate(corpus_path, verifier)
        return 0

    from readie_pipeline.corpus.generate import generate  # noqa: PLC0415 - optional extra

    if args.total:
        corpus_settings = replace(corpus_settings, total=args.total)
    corpus, report = generate(
        corpus_settings,
        settings.flavor,
        corpus_path,
        settings.corpus_cache_path,
        only_sources=only_sources,
        only_domains=only_domains,
        known_packages=_installed_packages(settings),
        exclude=tuple(args.exclude or ()),
    )
    _report_generation(corpus, report, corpus_path)
    return 0


def _report_generation(corpus: Corpus, report: Report, corpus_path: Path) -> None:
    """Print what a generation run produced."""
    print(f"[*] corpus now holds {len(corpus)} items at {corpus_path}")
    for name, count in corpus.source_counts().items():
        print(f"    {name}: {count}")
    usage = corpus.package_usage()
    if usage:
        top = ", ".join(f"{name} {count}" for name, count in list(usage.items())[:10])
        single = sum(1 for count in usage.values() if count == 1)
        print(f"    most used packages (items): {top}")
        print(f"    packages used by a single item: {single} of {len(usage)}")
    if report.shortfall:
        print(f"[!] {sum(report.shortfall.values())} items short of target:")
        for (domain, source), missing in sorted(report.shortfall.items()):
            print(f"    {source.value} / {domain}: {missing}")
    for reason, count in sorted(report.rejected.items()):
        print(f"    rejected ({reason}): {count}")
    if report.cleaned or report.rewrites:
        print(f"    cleaned: {report.cleaned} items had notebook syntax or paths fixed")
    for outcome, count in sorted(report.rewrites.items()):
        print(f"    stand-in rewrite ({outcome}): {count}")


def _clean(settings: Settings, args: argparse.Namespace, corpus_path: Path) -> int:
    """Make an existing corpus runnable, in place."""
    from readie_pipeline.corpus.clean import RewriteLog, clean_corpus  # noqa: PLC0415
    from readie_pipeline.corpus.verify import ResourceLookupError  # noqa: PLC0415

    standins = verifier = None
    workers = 6
    if not args.no_rewrite:
        from readie_pipeline.corpus.live import (  # noqa: PLC0415 - optional extra
            build_standins,
            build_verifier,
        )

        corpus_settings = CorpusSettings.from_env()
        workers = corpus_settings.rewrite_workers
        if corpus_settings.rewrite:
            standins = build_standins(corpus_settings)
            verifier = build_verifier(
                corpus_settings, settings.corpus_cache_path, _installed_packages(settings)
            )

    def verify(new: tuple[str, ...]) -> list[str] | str:
        assert verifier is not None  # noqa: S101 - only called when rewriting
        try:
            return verifier.missing(new)
        except (ResourceLookupError, CorpusError) as exc:
            return str(exc)

    log = RewriteLog(settings.corpus_cache_path.with_name("rewrites.json"))
    report = clean_corpus(
        corpus_path,
        standins=standins,
        verify=verify if standins is not None else None,
        log=log,
        workers=workers,
        retry=args.retry_rewrites,
    )
    if verifier is not None:
        verifier.flush()
    print(f"[*] cleaned {report.items} items at {corpus_path}")
    print(f"    notebook syntax or paths fixed in {report.changed} items")
    for fix, count in report.fixes.most_common():
        print(f"      {fix}: {count}")
    for outcome, count in sorted(report.rewrites.items()):
        print(f"    stand-in rewrite ({outcome}): {count}")
    print(f"    still reading a file that does not exist: {report.still_missing} items")
    print(f"    still with a cell that does not parse: {report.unparsable} items")
    return 0


def _installed_packages(settings: Settings) -> frozenset[str]:
    """Top-level names measured in this flavor's base image, if it was analyzed.

    Corpus code runs in that image, so a package installed there is real
    whatever its PyPI project is called. Empty when ``analyze`` has not run.
    """
    if not settings.metadata_path.exists():
        return frozenset()
    return frozenset(Metadata.load(settings.metadata_path).packages)


def cmd_analyze(settings: Settings, args: argparse.Namespace) -> int:
    """Measure every package installed here, plus every import the corpus uses.

    ``installed_packages()`` alone only ever names top-level packages -- it
    can never surface a specific dotted import like ``sklearn.svm``, since
    nothing else would ask to resolve or measure one. The corpus's raw
    ``package:`` resources are exactly those candidates (see ``CorpusItem.imports``), so
    both are measured together: the base image's own universe (a checkpoint
    restores into the base image, not into whatever a sample of requests
    happens to reference, so the planner needs a real number for all of it,
    corpus or not) plus whatever specific dotted names the corpus actually
    references. Run this wherever that base image's packages are actually
    importable (see ``installed_packages``).
    """
    packages = set(installed_packages())
    corpus: Corpus | None = None
    if settings.corpus_path.exists():
        corpus = Corpus.load(settings.corpus_path)
        for item in corpus:
            packages.update(item.imports)

    exclude = list(args.exclude or ())
    if exclude:
        print(f"[*] excluding: {', '.join(exclude)}")
    print(f"[*] analysing {len(packages)} candidate imports in this environment")

    def progress(name: str, facts: object) -> None:
        # An error is worth seeing on every run, verbose or not, because the
        # package it names is about to be dropped from the written metadata
        # rather than recorded -- this is the only place that reason surfaces
        # at all. A successful measurement is only noise unless asked for.
        error = getattr(facts, "error", "")
        if error:
            print(f"    {name}: {error}", file=sys.stderr)
        elif args.verbose:
            print(
                f"    {name}: {getattr(facts, 'disk_size_mb', 0.0):.1f} MB disk, "
                f"{getattr(facts, 'memory_size_mb', 0.0):.1f} MB resident, "
                f"{getattr(facts, 'import_time', 0.0):.3f} s"
            )

    metadata = replace(
        analyze(packages, exclude=exclude, on_progress=progress), base_image=settings.base_image
    )
    if corpus is not None:
        dataset_slugs = {
            dataset for item in corpus for dataset in item.of_kind(ResourceKind.KAGGLE_DATASET)
        }
        if dataset_slugs:
            dataset_facts = measure_datasets(sorted(dataset_slugs), on_progress=progress)
            metadata = replace(
                metadata,
                packages={**metadata.packages, **dataset_facts},
            )

    settings.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    settings.metadata_path.write_text(json.dumps(metadata.to_json(), indent=2) + "\n")

    usable = sum(1 for f in metadata.packages.values() if f.usable)
    print(f"[*] {usable}/{len(metadata)} packages measured -> {settings.metadata_path}")
    print(f"[*] versions recorded for {len(metadata.distributions)} distributions")
    print(f"[*] measured in {settings.base_image or 'an image that was not named'}")
    return 0


def _plan(settings: Settings) -> list[CheckpointPlan]:
    """Run the planner, then write the plan and the distributions it needs."""
    corpus = Corpus.load(settings.corpus_path)
    metadata = Metadata.load(settings.metadata_path)

    print(f"[*] planning over {len(corpus)} requests and {len(metadata)} packages")
    for domain, count in list(corpus.domain_counts().items())[:5]:
        print(f"    {domain}: {count}")

    planner = build_planner(settings.planner, settings.alpha)
    plans = planner.plan(
        corpus,
        metadata,
        Budget(
            max_checkpoints=settings.max_checkpoints,
            size_mb=settings.checkpoint_size_budget_mb,
        ),
    )

    for i, plan in enumerate(plans, start=1):
        print(
            f"[*] checkpoint {i}: {len(plan.imports)} packages, "
            f"{plan.requests_served} requests, {plan.seconds_saved:.0f}s saved, "
            f"{plan.size_mb:.0f} MB"
        )
        print(f"    {', '.join(plan.imports)}")

    write_plan(settings.plan_path, [p.to_json() for p in plans])
    print(f"[*] plan written to {settings.plan_path}")

    # What a rootfs built from a minimal base has to install for these imports to
    # work. Written for any planner, since it only reads the plan.
    distributions = distributions_for(plans, metadata)
    write_requirements(settings.requirements_path, requirement_lines(distributions, metadata))
    print(
        f"[*] {len(distributions)} distributions for the rootfs written to "
        f"{settings.requirements_path}"
    )
    return list(plans)


def _reuse_plan(settings: Settings) -> list[CheckpointPlan]:
    """Read the plan an earlier `plan` run wrote, instead of planning again.

    The planner, the corpus and the planner settings are not consulted: the file is
    the plan. Nothing is written back, so the file (and the rootfs-requirements.txt
    that was derived from it) stays exactly as the earlier run left it.
    """
    if not settings.plan_path.is_file():
        msg = f"no plan at {settings.plan_path} to reuse; run `readie-pipeline plan` first"
        raise ConfigError(msg)
    plans = [CheckpointPlan.from_json(e) for e in json.loads(settings.plan_path.read_text())]
    print(f"[*] reusing the {len(plans)} checkpoints planned in {settings.plan_path}")
    return plans


def cmd_plan(settings: Settings, args: argparse.Namespace) -> int:
    """Choose checkpoint contents and write the sandbox spec."""
    if getattr(args, "reuse_plan", False):
        _reuse_plan(settings)
    else:
        _plan(settings)

    if not args.no_spec:
        # Needs the ocispec binary and the rootfs, so --no-spec is what lets the
        # planning stage run anywhere -- including in a unit test.
        fingerprint = build_config(settings)
        settings.fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
        settings.fingerprint_path.write_text(fingerprint + "\n")
        print(f"[*] spec fingerprint {fingerprint}")

    # Beside the plan, so the host can see which of the plan's distributions the image
    # build could not install. Nothing happens where there is no rootfs (a host `plan`).
    skipped = record_skipped(settings.rootfs_path, settings.skipped_path)
    if skipped:
        print(
            f"[!] {len(skipped)} distributions did not install into the rootfs "
            f"(listed in {settings.skipped_path}): {', '.join(skipped)}"
        )

    return 0


def cmd_build(settings: Settings, args: argparse.Namespace) -> int:
    """Capture a checkpoint for each planned set."""
    del args

    if not settings.plan_path.exists():
        msg = f"no plan at {settings.plan_path}; run `readie-pipeline plan` first"
        raise ConfigError(msg)

    # Remove all contents but keep the top-level directory
    for item in Path(settings.output_dir).iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    plans = [CheckpointPlan.from_json(e) for e in json.loads(settings.plan_path.read_text())]
    if settings.planner != "fixed":
        plans = [CheckpointPlan(), *plans]

    fingerprint = settings.fingerprint_path.read_text().strip()
    version = runsc_version(settings.runsc_binary)
    metadata = Metadata.load(settings.metadata_path)

    print(f"[*] building {len(plans)} checkpoints with {version}")

    entries: list[tuple[str, CheckpointPlan]] = []
    sizes_mb: list[float] = []
    restore_times: list[float] = []
    for index, plan in enumerate(plans):
        checkpoint_id = f"checkpoint_{index}"
        print(f"[*] {checkpoint_id}: {', '.join(plan.imports) or '(nothing pre-imported)'}")

        def write_spec(mode: ExecutorMode, checkpoint_dir: Path, preimport: str) -> None:
            # The fingerprint is discarded here on purpose: only process.env
            # differs between the checkpoints of one generation, and
            # runsc.Fingerprint excludes env, so it is the value already
            # recorded by `plan`.
            build_config(settings, checkpoint_dir, mode=mode, preimport=preimport)

        destination = capture(settings, checkpoint_id, plan, write_spec=write_spec)
        CheckpointMeta(
            checkpoint_id=checkpoint_id,
            runsc_version=version,
            spec_fingerprint=fingerprint,
            imports=plan.imports,
            datasets=plan.datasets,
            models=plan.models,
            tokenizers=plan.tokenizers,
        ).write(destination)
        entries.append((checkpoint_id, plan))
        print(f"    captured at {destination}")

        restore_time = measure_time(settings, checkpoint_id, write_spec=write_spec)
        print(f"    restored in {restore_time}s")
        sizes_mb.append(plan.size_mb)
        restore_times.append(restore_time)

    Manifest(
        runsc_version=version,
        spec_fingerprint=fingerprint,
        python_path=settings.rootfs_pythonpath,
        overlay=settings.sandbox_overlay,
        network=settings.sandbox_network,
    ).write(settings.output_dir)

    # alpha is the seconds-per-MB *slope* of restore_time vs. size_mb -- a
    # linear relationship, but not one that passes through the origin: even
    # the empty checkpoint (index 0, size_mb == 0) takes real, nonzero time
    # to restore (a fixed gVisor sandbox-startup cost, unrelated to size).
    # Fitting a per-checkpoint ratio (restore_time - baseline_time) / size_mb
    # and averaging those, as this used to, implicitly forces that fixed
    # cost to be treated as part of the size-proportional term -- for a
    # checkpoint whose size_mb is small relative to the fixed cost, that
    # ratio is dominated by the fixed cost alone and blows up (confirmed
    # against real data: a 10MB checkpoint produced a ratio 15x every other
    # checkpoint's, dragging the unweighted mean far above what the other,
    # larger checkpoints actually showed). A single ordinary-least-squares
    # fit across every (size_mb, restore_time) pair separates the two
    # unavoidably-confounded terms properly: its slope is alpha, robust to
    # small checkpoints since they just become low-leverage points near the
    # intercept instead of a division problem; its intercept is the fixed
    # cost itself, discarded here since it never affects which checkpoint
    # the router or planner ends up choosing (the same constant added to
    # every candidate never changes an argmin).
    computed_alpha = settings.alpha
    try:
        if len(plans) > 0:
            computed_alpha = round(
                float(np.polyfit(sizes_mb, restore_times, 1)[0]), ALPHA_PRECISION
            )
    except np.linalg.LinAlgError as exc:
        msg = f"cannot compute alpha from {len(sizes_mb)} checkpoints: {exc}"
        print(f"[!] {msg}; using configured alpha {computed_alpha}")
    drift = computed_alpha - settings.alpha
    print(
        f"\n[*] planned alpha value: {settings.alpha}, "
        f"computed alpha: {computed_alpha}, drift: {drift}"
    )
    # The catalogue the router selects from: every measured item's cost plus each
    # checkpoint's contents and precomputed size term. The metadata may have been
    # measured in a different image than the rootfs the sandbox runs, so the
    # catalogue only ports what that rootfs provides (same discovery as `analyze`).
    inventory = discover_rootfs_inventory(settings.rootfs_path, settings.rootfs_pythonpath)
    write_catalogue(
        settings.catalogue_path,
        build_catalogue(
            flavor=settings.flavor,
            alpha=computed_alpha,
            metadata=metadata,
            entries=entries,
            inventory=inventory,
        ),
    )

    print(f"[*] captured {len(plans)} checkpoints into {settings.output_dir}")
    print("[*] `make generation` bakes these into a worker image alongside the rootfs")
    return 0


def cmd_capture(settings: Settings, args: argparse.Namespace) -> int:
    """Plan and capture in one process.

    The two stages have to share a container. `plan` writes the bundle's
    config.json under BASE_DIR, which lives in the image and is not mounted out,
    so a second `docker run` would start from a bundle with no spec in it. This
    is what the image's CMD is, and running `plan` at build time instead does not
    work: the capture mounts a host directory over the output path, and a bind
    mount hides whatever the image put there.
    """
    return cmd_plan(settings, args) or cmd_build(settings, args)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def add_planner_flags(parser: argparse.ArgumentParser) -> None:
    """Attach the planner's knobs, shared by `plan` and `capture`."""
    parser.add_argument("--planner", choices=("greedy", "fixed"), help="selection strategy")
    parser.add_argument("--max-checkpoints", type=int, help="how many checkpoints to plan")
    parser.add_argument(
        "--reuse-plan",
        action="store_true",
        help="skip planning and use the checkpoints.json an earlier `plan` run wrote",
    )
    parser.add_argument(
        "--size-budget-mb", type=float, help="total size budget across all checkpoints, in MB"
    )


def build_parser() -> argparse.ArgumentParser:
    """Assemble the argument parser."""
    parser = argparse.ArgumentParser(
        prog="readie-pipeline",
        description="Analyse the request corpus and capture gVisor checkpoints.",
    )
    parser.add_argument("--data-dir", type=Path, help="corpus and metadata location")
    parser.add_argument("--output-dir", type=Path, help="where build outputs are written")
    parser.add_argument("--bundle-dir", type=Path, help="the OCI bundle")
    parser.add_argument(
        "--plan-dir", type=Path, help="where the plan goes, if not with the outputs"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    corpus = sub.add_parser(
        "corpus",
        help="build the corpus for FLAVOR from Kaggle, Hugging Face and the hosted model",
    )
    corpus.add_argument("--total", type=int, help="new items to append to the corpus")
    corpus.add_argument(
        "--source",
        action="append",
        choices=[s.value for s in Source],
        help="restrict to a source; repeatable",
    )
    corpus.add_argument("--domain", action="append", help="restrict to a domain; repeatable")
    corpus.add_argument(
        "--dry-run", action="store_true", help="print the planned mix and fetch nothing"
    )
    corpus.add_argument(
        "--output",
        type=Path,
        help="the corpus file to read and write instead of data/datasets/<flavor>.json; "
        "this is how an eval set is generated",
    )
    corpus.add_argument(
        "--exclude",
        action="append",
        type=Path,
        help="a corpus file whose items nothing added may repeat; repeatable",
    )
    corpus.add_argument(
        "--clean",
        action="store_true",
        help="make the items of an existing corpus runnable, in place: notebook syntax, "
        "Kaggle folders, and stand-in data for reads of files that do not exist",
    )
    corpus.add_argument(
        "--no-rewrite",
        action="store_true",
        help="skip the model rewrite of missing-file reads, for --clean and for generation",
    )
    corpus.add_argument(
        "--retry-rewrites",
        action="store_true",
        help="with --clean, offer the model the items it already failed to rewrite again",
    )
    corpus.add_argument(
        "--revalidate",
        action="store_true",
        help="re-verify every resource in the existing corpus and drop items that fail",
    )
    corpus.set_defaults(handler=cmd_corpus)

    analyze_cmd = sub.add_parser("analyze", help="measure package sizes and import times")
    analyze_cmd.add_argument(
        "-v", "--verbose", action="store_true", help="also report each successful measurement"
    )
    analyze_cmd.add_argument(
        "--exclude",
        action="append",
        help="skip a package, and everything under it; repeatable",
    )
    analyze_cmd.set_defaults(handler=cmd_analyze)

    plan = sub.add_parser("plan", help="choose checkpoint contents and write the spec")
    add_planner_flags(plan)
    plan.add_argument(
        "--no-spec",
        action="store_true",
        help="skip generating config.json, which needs the ocispec binary",
    )
    plan.set_defaults(handler=cmd_plan)

    build = sub.add_parser("build", help="capture a checkpoint for each planned set")
    build.set_defaults(handler=cmd_build)

    capture_cmd = sub.add_parser("capture", help="plan and then capture, in one process")
    add_planner_flags(capture_cmd)
    # no_spec is not offered: a capture needs the spec `plan` writes, and the
    # flag exists only so planning can run where the ocispec binary does not.
    capture_cmd.set_defaults(handler=cmd_capture, no_spec=False)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the pipeline. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    try:
        settings = Settings.from_env(
            data_dir=getattr(args, "data_dir", None),
            output_dir=getattr(args, "output_dir", None),
            bundle_dir=getattr(args, "bundle_dir", None),
            plan_dir=getattr(args, "plan_dir", None),
            planner=getattr(args, "planner", None),
            max_checkpoints=getattr(args, "max_checkpoints", None),
            checkpoint_size_budget_mb=getattr(args, "size_budget_mb", None),
        )
        return int(args.handler(settings, args))
    except (ConfigError, CorpusError, MetadataError, SpecError, CaptureError) as exc:
        # Every expected failure is one of these, and each already explains
        # itself. A traceback here would bury the explanation in frames from
        # argparse.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
