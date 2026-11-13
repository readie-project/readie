"""Append completed resource measurements and resolutions to a resumable journal.

Each entry is flushed before the coordinator moves on. Finished metadata is replaced
only after analysis succeeds. Journal policy is independent of resource type.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any, Protocol

from readie_pipeline.metadata.models import Metadata, MetadataError, ResourceFacts

PARTIAL_SUFFIX = ".partial"

#: How many differing distributions ``incompatibility`` names before it says "and more".
_NAMED = 3


def partial_path(metadata_path: Path) -> Path:
    """Where an unfinished run saves, beside the metadata file it will produce."""
    return metadata_path.with_name(metadata_path.name + PARTIAL_SUFFIX)


class Journal(Protocol):
    """Receives each result as it is reached. A seam for ``analyze``."""

    def resource(self, name: str, facts: ResourceFacts) -> None:
        """Persist a resource outcome, including failures, before returning."""
        ...

    def resolution(self, raw: str, resolved: str) -> None:
        """Record a resource candidate and its canonical ID."""
        ...


class ProgressJournal:
    """Appends each result to a side file, one JSON object per line, flushed to disk.

    A line is written and synced before the next package is measured. A crash can leave the
    last line cut short, which ``read_progress`` skips, and loses nothing earlier.
    """

    def __init__(
        self,
        path: Path,
        *,
        samples: int,
        base_image: str,
        installed: Mapping[str, str],
        resume: bool,
    ) -> None:
        """Open the file.

        With ``resume`` an existing file is kept and added to. Otherwise it is started again,
        with a first line that records what the run measures with.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        keep = resume and path.exists()
        self._file: IO[str] = path.open("a" if keep else "w")
        if not keep:
            header = {"samples": samples, "base_image": base_image, "installed": dict(installed)}
            self._append({"header": header})

    def resource(self, name: str, facts: ResourceFacts) -> None:
        """Persist successes for resume and failures for diagnostics, without caching failures."""
        if facts.usable:
            self._append({"resource": name, "facts": facts.to_json()})
        else:
            record = {"failure": name, "facts": facts.to_json(), "error": facts.error}
            self._append(record)
            failure_path = self._path.with_name(self._path.name + ".failures.jsonl")
            with failure_path.open("a") as stream:
                stream.write(json.dumps(record) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def resolution(self, raw: str, resolved: str) -> None:
        """Record what ``raw`` resolved to."""
        self._append({"resolved": [raw, resolved]})

    def close(self) -> None:
        """Close the file, leaving it for a later run to continue from."""
        self._file.close()

    def remove(self) -> None:
        """Close and delete the file, once the run has produced its real result."""
        self.close()
        self._path.unlink(missing_ok=True)

    def _append(self, record: Mapping[str, Any]) -> None:
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())


def read_progress(path: Path) -> tuple[Metadata, int]:
    """Load an unfinished run, with the number of samples its timings were taken with.

    Lines that cannot be read are skipped, which is what a crash in the middle of a write
    leaves; the package on such a line is simply measured again. Raises ``MetadataError`` for
    a file that does not start with the header of an unfinished run.
    """
    lines = path.read_text().splitlines()
    try:
        header = json.loads(lines[0])["header"]
        samples = int(header["samples"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        msg = f"{path} is not an unfinished analyze run: it has no readable header"
        raise MetadataError(msg) from exc

    packages: dict[str, ResourceFacts] = {}
    resolved: dict[str, str] = {}
    for line in lines[1:]:
        try:
            record = json.loads(line)
            if "resource" in record:
                packages[record["resource"]] = ResourceFacts.from_json(
                    record["resource"], record["facts"]
                )
            elif "resolved" in record:
                raw, target = record["resolved"]
                resolved[str(raw)] = str(target)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, MetadataError):
            continue
    metadata = Metadata(
        packages,
        distributions={str(k): str(v) for k, v in (header.get("installed") or {}).items()},
        base_image=str(header.get("base_image") or ""),
        resolved=resolved,
    )
    return metadata, samples


def incompatibility(known: Metadata, *, base_image: str, installed: Mapping[str, str]) -> str:
    """Why ``known`` cannot be reused in this environment, or an empty string if it can.

    What it recorded about each package, the module it resolves to and the modules it loads,
    holds only for the image and the package versions it was measured with.
    """
    if base_image and known.base_image and base_image != known.base_image:
        return f"it was measured in image {known.base_image}, not {base_image}"
    if not known.distributions:
        return "it records no distribution versions to compare with"
    differing = sorted(
        f"{name} {version} (now {installed.get(name) or 'not installed'})"
        for name, version in known.distributions.items()
        if installed.get(name) != version
    )
    if differing:
        shown = ", ".join(differing[:_NAMED]) + (", ..." if len(differing) > _NAMED else "")
        return f"{len(differing)} distribution version(s) differ: {shown}"
    return ""
