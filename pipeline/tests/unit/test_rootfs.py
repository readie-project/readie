"""Listing what the rootfs a sandbox runs provides."""

from __future__ import annotations

import subprocess
from pathlib import Path

from readie_pipeline.capture.rootfs import discover_rootfs_inventory, record_skipped
from readie_pipeline.metadata import inventory as inventory_module
from readie_pipeline.metadata.inventory import Inventory

# ROOTFS_PYTHONPATH as the Dockerfile sets it: where the Kaggle rootfs keeps its
# packages, and a prefix the sandbox adds to sys.path.
PYTHONPATH = "/usr/local/lib/python3.12/dist-packages"
ROOTFS = Path("/var/lib/readie/rootfs")


class _Interpreter:
    """A stand-in for `chroot <rootfs> ... python -`, answering with this output."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.argv: list[str] = []
        self.input = ""

    def __call__(self, argv: list[str], **kwargs: str) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        self.input = kwargs["input"]
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def _inventory_json() -> str:
    return Inventory(provided={"numpy": ["numpy"]}, distributions=frozenset({"numpy"})).to_json()


def _rootfs_with_skipped(tmp_path: Path, text: str | None) -> Path:
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    if text is not None:
        (rootfs / "etc" / "readie-rootfs-skipped.txt").write_text(text)
    return rootfs


def test_the_skipped_distributions_are_copied_beside_the_plan(tmp_path: Path):
    rootfs = _rootfs_with_skipped(tmp_path, "scikit-learn\ntorch\n")
    destination = tmp_path / "plan" / "rootfs-skipped.txt"

    skipped = record_skipped(rootfs, destination)

    assert skipped == ("scikit-learn", "torch")
    lines = destination.read_text().splitlines()
    assert lines[0].startswith("#")
    assert lines[-2:] == ["scikit-learn", "torch"]


def test_nothing_skipped_leaves_a_file_with_only_its_header(tmp_path: Path):
    # An empty file is how a person tells "everything installed" from "no record".
    destination = tmp_path / "rootfs-skipped.txt"

    assert record_skipped(_rootfs_with_skipped(tmp_path, ""), destination) == ()
    assert all(line.startswith("#") for line in destination.read_text().splitlines())


def test_a_rootfs_with_no_record_writes_nothing(tmp_path: Path):
    destination = tmp_path / "rootfs-skipped.txt"

    assert record_skipped(_rootfs_with_skipped(tmp_path, None), destination) is None
    assert not destination.exists()


def test_the_rootfs_python_runs_the_inventory_module_with_the_sandboxs_pythonpath():
    # Pinned: ROOTFS_PYTHONPATH once named a directory that did not exist in the
    # real image, so looking only there found nothing. The rootfs python is asked.
    run = _Interpreter(_inventory_json() + "\n")

    inventory = discover_rootfs_inventory(ROOTFS, PYTHONPATH, run=run)

    assert run.argv == [
        "chroot",
        str(ROOTFS),
        "env",
        f"PYTHONPATH={PYTHONPATH}",
        "python",
        "-",
    ]
    assert run.input == Path(inventory_module.__file__).read_text(), "fed on stdin, not copied in"
    assert inventory is not None
    assert inventory.distributions == {"numpy"}
    assert inventory.provided == {"numpy": ["numpy"]}


def test_a_failing_interpreter_is_logged_and_gives_no_inventory(capsys):
    run = _Interpreter(returncode=127, stderr="python: not found")

    assert discover_rootfs_inventory(ROOTFS, PYTHONPATH, run=run) is None
    assert "python: not found" in capsys.readouterr().out


def test_an_empty_inventory_is_logged_and_treated_as_unknown(capsys):
    # Every measured package would look absent and the catalogue would have no
    # items, which is what the first, path-guessing discovery silently produced.
    empty = Inventory(provided={}, distributions=frozenset()).to_json()

    assert discover_rootfs_inventory(ROOTFS, PYTHONPATH, run=_Interpreter(empty)) is None
    assert "reports no packages" in capsys.readouterr().out


def test_unreadable_output_is_logged_and_gives_no_inventory(capsys):
    assert discover_rootfs_inventory(ROOTFS, PYTHONPATH, run=_Interpreter("garbage")) is None
    assert "unreadable" in capsys.readouterr().out
