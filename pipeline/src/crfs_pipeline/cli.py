"""The pipeline's command line.

Four stages, each runnable alone::

    crfs-pipeline corpus    generate request snippets with a hosted model
    crfs-pipeline analyze   measure package sizes and import times
    crfs-pipeline plan      choose checkpoint contents, write the OCI spec
    crfs-pipeline build     capture one gVisor checkpoint per planned set

plus ``capture``, which is ``plan`` and ``build`` in one process because the
image's entrypoint needs them to share a container. See :func:`cmd_capture`.

This replaces ``setup.py`` and ``main.py``, which were module-scope scripts:
they executed on *import*, so neither could be tested, and neither stage could
be run without the other.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from crfs_pipeline.capture.build import CaptureError, capture, measure_time
from crfs_pipeline.capture.spec import SpecError, build_config, runsc_version, ExecutorMode
from crfs_pipeline.catalogue import build_catalogue, write_catalogue
from crfs_pipeline.config import ConfigError, CorpusSettings, Settings
from crfs_pipeline.corpus.models import Corpus, CorpusError
from crfs_pipeline.manifest import CheckpointMeta, Manifest, write_plan
from crfs_pipeline.metadata.analyze import analyze
from crfs_pipeline.metadata.models import Metadata, MetadataError
from crfs_pipeline.planning.ports import Budget, CheckpointPlan, CheckpointPlanner


def build_planner(name: str, alpha: float) -> CheckpointPlanner:
    """Resolve a planner by name.

    ``alpha`` is the shared size-vs-time weight; only the greedy planner uses it.
    """
    from crfs_pipeline.planning.fixed import FixedPlanner  # noqa: PLC0415 - avoids an import cycle

    if name == "fixed":
        return FixedPlanner()
    if name == "greedy":
        from crfs_pipeline.planning.greedy import GreedyPlanner  # noqa: PLC0415

        return GreedyPlanner(alpha=alpha)

    msg = f"unknown planner {name!r}; choose from fixed, greedy"
    raise ConfigError(msg)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------
def cmd_corpus(settings: Settings, args: argparse.Namespace) -> int:
    """Generate request snippets."""
    from crfs_pipeline.corpus.generate import generate  # noqa: PLC0415 - optional extra

    corpus_settings = CorpusSettings.from_env()
    corpus = generate(
        corpus_settings,
        settings.corpus_path,
        categories=args.category or None,
    )
    print(f"[*] corpus now holds {len(corpus)} requests at {settings.corpus_path}")
    return 0


def cmd_analyze(settings: Settings, args: argparse.Namespace) -> int:
    """Measure the packages the corpus refers to."""
    corpus = Corpus.load(settings.corpus_path)
    packages = sorted(corpus.resources().packages)
    print(f"[*] analysing {len(packages)} packages from {len(corpus)} requests")

    def progress(name: str, facts: object) -> None:
        detail = getattr(facts, "error", "") or (
            f"{getattr(facts, 'disk_size_mb', 0.0):.1f} MB, "
            f"{getattr(facts, 'import_time', 0.0):.3f} s"
        )
        print(f"    {name}: {detail}")

    metadata = analyze(packages, on_progress=progress if args.verbose else None)

    settings.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    settings.metadata_path.write_text(json.dumps(metadata.to_json(), indent=2) + "\n")

    usable = sum(1 for f in metadata.packages.values() if f.usable)
    print(f"[*] {usable}/{len(metadata)} packages measured -> {settings.metadata_path}")
    return 0


def cmd_plan(settings: Settings, args: argparse.Namespace) -> int:
    """Choose checkpoint contents and write the sandbox spec."""
    corpus = Corpus.load(settings.corpus_path)
    metadata = Metadata.load(settings.metadata_path)

    print(f"[*] planning over {len(corpus)} requests and {len(metadata)} packages")
    for category, count in list(corpus.category_counts().items())[:5]:
        print(f"    {category}: {count}")

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

    if not args.no_spec:
        # Needs the ocispec binary and the rootfs, so --no-spec is what lets the
        # planning stage run anywhere -- including in a unit test.
        fingerprint = build_config(settings)
        settings.fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
        settings.fingerprint_path.write_text(fingerprint + "\n")
        print(f"[*] spec fingerprint {fingerprint}")

    return 0


def cmd_build(settings: Settings, args: argparse.Namespace) -> int:
    """Capture a checkpoint for each planned set."""
    del args

    if not settings.plan_path.exists():
        msg = f"no plan at {settings.plan_path}; run `crfs-pipeline plan` first"
        raise ConfigError(msg)

    plans = [CheckpointPlan()] + [CheckpointPlan.from_json(e) for e in json.loads(settings.plan_path.read_text())]
    fingerprint = settings.fingerprint_path.read_text().strip()
    version = runsc_version(settings.runsc_binary)
    metadata = Metadata.load(settings.metadata_path)

    print(f"[*] building {len(plans)} checkpoints with {version}")

    entries: list[tuple[str, CheckpointPlan]] = []
    baseline_time = 0.0
    computed_alphas: list[float] = []
    for index, plan in enumerate(plans, start=1):
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
        if index == 0:
            baseline_time = restore_time
        computed_alphas.append((restore_time - baseline_time) / plan.size_mb if plan.size_mb else 0.0)

    Manifest(
        runsc_version=version,
        spec_fingerprint=fingerprint,
        python_path=settings.rootfs_pythonpath,
        overlay=settings.sandbox_overlay,
        network=settings.sandbox_network,
    ).write(settings.output_dir)

    computed_alpha = np.mean(computed_alphas)
    print(f"\n[*] planned alpha value: {settings.alpha}, computed alpha: {computed_alpha}, drift: {computed_alpha - settings.alpha}")
    # The catalogue the router selects from: every measured item's cost plus each
    # checkpoint's contents and precomputed size term.
    write_catalogue(
        settings.catalogue_path,
        build_catalogue(
            flavor=settings.flavor,
            alpha=computed_alpha,
            metadata=metadata,
            entries=entries,
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
    parser.add_argument("--size-budget-mb", type=float, help="per-checkpoint size budget")


def build_parser() -> argparse.ArgumentParser:
    """Assemble the argument parser."""
    parser = argparse.ArgumentParser(
        prog="crfs-pipeline",
        description="Analyse the request corpus and capture gVisor checkpoints.",
    )
    parser.add_argument("--data-dir", type=Path, help="corpus and metadata location")
    parser.add_argument("--output-dir", type=Path, help="where build outputs are written")
    parser.add_argument("--bundle-dir", type=Path, help="the OCI bundle")
    parser.add_argument(
        "--plan-dir", type=Path, help="where the plan goes, if not with the outputs"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    corpus = sub.add_parser("corpus", help="generate request snippets with a hosted model")
    corpus.add_argument("--category", action="append", help="restrict to a category; repeatable")
    corpus.set_defaults(handler=cmd_corpus)

    analyze_cmd = sub.add_parser("analyze", help="measure package sizes and import times")
    analyze_cmd.add_argument("-v", "--verbose", action="store_true", help="report each package")
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
