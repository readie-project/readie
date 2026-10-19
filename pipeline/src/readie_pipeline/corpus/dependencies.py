"""What a piece of code needs from the machine it runs on.

A static scan, never executed: it parses each step and looks at the strings and
calls in it. It finds reads of files that will not exist in a sandbox (the
dominant reason fetched code fails there), plus network use, environment
variables and logins, which are only reported.

Literals are all it can see. A path built from variables or an f-string is missed,
so the result is a floor on what a piece of code depends on, never a guarantee.
"""

from __future__ import annotations

import ast
import posixpath
import warnings
from collections.abc import Sequence
from dataclasses import dataclass

from readie_pipeline.corpus.runnable import strip_prelude

MISSING_INPUT = "missing_input"
NETWORK = "network"
SECRET = "secret"  # noqa: S105 - a category name, not a password
UNPARSABLE = "unparsable"

_READ_CALLS = frozenset(
    {
        "read_csv", "read_json", "read_parquet", "read_excel", "read_table", "read_feather",
        "read_pickle", "read_hdf", "read_fwf", "read_stata", "read_sas", "read_text",
        "read_bytes", "open", "load", "loadtxt", "genfromtxt", "imread", "listdir", "glob",
        "walk", "scandir", "load_img", "load_model", "VideoCapture", "ImageFolder",
        "image_dataset_from_directory", "flow_from_directory", "iterdir",
    }
)  # fmt: skip
_WRITE_CALLS = frozenset(
    {
        "to_csv", "to_json", "to_parquet", "to_excel", "to_pickle", "to_feather", "to_hdf",
        "save", "savetxt", "savez", "savez_compressed", "dump", "write_text", "write_bytes",
        "imwrite", "to_sql", "save_image", "touch", "copy", "copyfile", "copytree", "move",
    }
)  # fmt: skip
_DATA_EXTENSIONS = (
    ".csv", ".tsv", ".json", ".jsonl", ".parquet", ".xls", ".xlsx", ".txt", ".npy", ".npz",
    ".pkl", ".pickle", ".h5", ".hdf5", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".wav", ".mp3",
    ".mp4", ".zip", ".gz", ".tar", ".feather", ".sav", ".dta", ".xml", ".db", ".sqlite", ".pt",
    ".pth", ".ckpt", ".joblib", ".yaml", ".yml",
)  # fmt: skip
_DIRECTORY_CREATION = frozenset({"makedirs", "mkdir"})
_DIRECTORY_READS = frozenset({"listdir", "glob", "walk", "scandir", "iterdir", "ImageFolder"})
_REMOTE = ("http://", "https://", "s3://", "gs://", "gcs://", "hf://")
_FOREIGN_ROOTS = ("/content/", "/mnt/", "/home/", "/data/", "/drive/", "C:\\", "D:\\")
_NETWORK_CALLS = frozenset(
    {
        "load_dataset", "from_pretrained", "hf_hub_download", "snapshot_download", "urlretrieve",
        "dataset_download", "model_download", "fetch_openml", "fetch_20newsgroups",
        "fetch_california_housing", "fetch_lfw_people", "fetch_covtype", "download",
    }
)  # fmt: skip
_SECRET_CALLS = frozenset(
    {"getenv", "get_secret", "get_secret_value", "login", "UserSecretsClient"}
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing a piece of code depends on."""

    kind: str
    cell: int
    detail: str


def scan(code: Sequence[str]) -> list[Finding]:
    """Everything the steps depend on that a bare sandbox does not provide.

    The prelude block is left out: it only creates the Kaggle folders, and it
    would otherwise look like code that reads ``/kaggle/input``.
    """
    trees = [_parse(strip_prelude(step)) for step in code]
    provided = _provided_paths([t for t in trees if t is not None])
    findings: list[Finding] = []
    for index, tree in enumerate(trees):
        if tree is None:
            findings.append(Finding(UNPARSABLE, index, ""))
            continue
        findings.extend(_scan_cell(index, tree, provided))
    return findings


def missing_inputs(code: Sequence[str]) -> dict[int, list[str]]:
    """Cells that read files nothing in the item creates, with the paths they read."""
    found: dict[int, list[str]] = {}
    for finding in scan(code):
        if finding.kind == MISSING_INPUT and finding.detail not in found.get(finding.cell, []):
            found.setdefault(finding.cell, []).append(finding.detail)
    return found


def normalise_path(path: str) -> str:
    """``../input/x`` and ``/kaggle/input/x`` name the same place from ``/kaggle/working``."""
    cleaned = path.replace("\\", "/")
    for prefix in ("./../input", "../input"):
        if cleaned.startswith(prefix):
            return "/kaggle/input" + cleaned[len(prefix) :]
    return cleaned


def _parse(source: str) -> ast.Module | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _call_name(node: ast.Call) -> tuple[str, str]:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr, ast.unparse(func.value)
    if isinstance(func, ast.Name):
        return func.id, ""
    return "", ""


def _first_string(node: ast.Call) -> str | None:
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _writes_to_file(node: ast.Call) -> bool:
    """``open(path, "w")`` and friends."""
    if _call_name(node)[0] != "open":
        return False
    mode = node.args[1] if len(node.args) > 1 else None
    if mode is None:
        mode = next((k.value for k in node.keywords if k.arg == "mode"), None)
    return (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and any(c in mode.value for c in "wax")
    )


def _provided_paths(trees: Sequence[ast.Module]) -> set[str]:
    """Literal paths the item itself creates, so reading them is not a missing input.

    Creating a folder does not count: an empty folder is not the data in it.
    """
    provided: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            path = _first_string(node)
            if path and (_call_name(node)[0] in _WRITE_CALLS or _writes_to_file(node)):
                provided.add(normalise_path(path))
    return provided


def _covered(path: str, provided: set[str]) -> bool:
    """Whether something the item creates is, or lies under or over, ``path``."""
    wanted = normalise_path(path).rstrip("/")
    for entry in provided:
        made = entry.rstrip("/")
        if made == wanted or made.startswith(wanted + "/") or wanted.startswith(made + "/"):
            return True
        if posixpath.dirname(made) == posixpath.dirname(wanted) and wanted.startswith(made):
            return True
    return False


def _creating_paths(tree: ast.Module) -> set[int]:
    """The path literals a cell passes to a call that creates something.

    ``os.makedirs('/kaggle/input/x')`` names the Kaggle input folder to make it, not
    to read from it, so it must not count as a dependency on it.
    """
    creating: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = _call_name(node)[0]
        if name in _WRITE_CALLS or name in _DIRECTORY_CREATION or _writes_to_file(node):
            creating.add(id(node.args[0]))
    return creating


def _scan_cell(index: int, tree: ast.Module, provided: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    creating = _creating_paths(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in creating:
                findings.extend(_path_finding(index, node.value, provided, read=False))
        elif isinstance(node, ast.Call):
            findings.extend(_call_findings(index, node, provided))
    return findings


def _path_finding(index: int, path: str, provided: set[str], *, read: bool) -> list[Finding]:
    """A finding for ``path`` when it names a file a bare sandbox does not have."""
    if path.startswith(_REMOTE):
        return []
    wanted = normalise_path(path)
    in_kaggle_input = wanted.startswith("/kaggle/input")
    foreign = path.startswith(_FOREIGN_ROOTS)
    data_file = path.lower().endswith(_DATA_EXTENSIONS)
    missing = (
        in_kaggle_input
        or (foreign and (read or data_file))
        or (read and (not path.startswith("/") or wanted.startswith("/kaggle/")))
    )
    if not missing or _covered(path, provided):
        return []
    return [Finding(MISSING_INPUT, index, path)]


def _call_findings(index: int, node: ast.Call, provided: set[str]) -> list[Finding]:
    name, owner = _call_name(node)
    path = _first_string(node)
    findings: list[Finding] = []
    if name in _READ_CALLS and path is not None:
        is_directory = name in _DIRECTORY_READS
        if is_directory or path.lower().endswith(_DATA_EXTENSIONS):
            findings.extend(_path_finding(index, path, provided, read=True))
    if name in _NETWORK_CALLS:
        findings.append(Finding(NETWORK, index, name))
    if name in {"get", "post"} and owner.endswith("requests"):
        findings.append(Finding(NETWORK, index, f"requests.{name}"))
    if name in _SECRET_CALLS or (name == "get" and owner.endswith("environ")):
        findings.append(Finding(SECRET, index, name))
    return findings
