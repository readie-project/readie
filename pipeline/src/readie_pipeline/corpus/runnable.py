"""Mechanical fixes that make notebook code run as plain Python.

Fetched notebooks are full of IPython syntax (``%matplotlib inline``,
``!pip install x``, ``%%time``). None of it parses, so the cell fails before it
does anything. The fixes here turn that syntax into ordinary Python, or into a
comment, and change nothing else.

Every change keeps the original text in a ``# [readie] ...`` comment, so nothing
is lost. A cell is only touched when it does not parse, and a change that leaves
it unparsable is thrown away, so the transform can never make a cell worse. The
whole pass is idempotent: running it on its own output changes nothing.
"""

from __future__ import annotations

import ast
import re
import warnings
from collections.abc import Sequence
from dataclasses import dataclass

NOTE = "# [readie]"
PRELUDE_START = "# [readie:prelude]"
PRELUDE_END = "# [readie:end]"

#: Magics that only configure the notebook or print something. Without a
#: notebook they do nothing, so they are commented out.
_DROPPED = frozenset(
    {
        "matplotlib", "load_ext", "reload_ext", "unload_ext", "config", "autoreload",
        "precision", "xmode", "colors", "pdb", "pprint", "lsmagic", "who", "who_ls", "whos",
        "reset", "clear", "history", "quickref", "magic", "rehashx", "automagic", "autocall",
        "alias", "killbgscripts", "notebook", "tb", "pwd", "ls", "dirs", "env_list",
    }
)  # fmt: skip
_INSTALLERS = frozenset({"pip", "conda", "mamba", "uv"})
_INSTALL_COMMANDS = frozenset({"pip", "pip3", "conda", "mamba", "apt", "apt-get", "sudo", "uv"})
_DROPPED_CELL_MAGICS = frozenset({"time", "timeit", "capture", "prun", "debug"})
_TIMEIT_OPTIONS = re.compile(r"^(?:-[a-zA-Z]\s*\d*\s*)+")
_IPYTHON_CALL = re.compile(
    r"^(?P<indent>\s*)get_ipython\(\)\.(?P<method>run_line_magic|magic|system)\("
    r"\s*(?P<q1>['\"])(?P<first>.*?)(?P=q1)\s*(?:,\s*(?P<q2>['\"])(?P<second>.*?)(?P=q2)\s*)?\)\s*$"
)
_ASSIGNED_SHELL = re.compile(r"^(\s*)([A-Za-z_][\w.]*)\s*=\s*!(.+)$")
_KAGGLE_PATHS = re.compile(r"/kaggle/|\.\./input")
_BRACED = re.compile(r"\{[A-Za-z_][\w.\[\]'\"]*\}")
_PRELUDE = f"""{PRELUDE_START}
import os
os.makedirs('/kaggle/working', exist_ok=True)
os.makedirs('/kaggle/input', exist_ok=True)
os.chdir('/kaggle/working')
{PRELUDE_END}
"""


@dataclass(frozen=True, slots=True)
class Cleaned:
    """The steps after cleaning, and what was done to them."""

    code: tuple[str, ...]
    changes: tuple[str, ...]


def parses(source: str) -> bool:
    """Whether ``source`` is valid Python."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    return True


def clean_steps(code: Sequence[str]) -> Cleaned:
    """Apply every mechanical fix to a list of steps."""
    steps: list[str] = []
    changes: list[str] = []
    for step in code:
        cleaned, done = clean_cell(step)
        steps.append(cleaned)
        changes.extend(done)

    with_prelude = _with_prelude(steps)
    if with_prelude is not None:
        steps, changes = with_prelude, [*changes, "prelude"]
    return Cleaned(tuple(steps), tuple(changes))


def clean_cell(source: str) -> tuple[str, tuple[str, ...]]:
    """Fix one cell. Returns it unchanged when it already parses or cannot be fixed."""
    lines = source.split("\n")
    changes: list[str] = []
    if parses(source):
        # Valid Python that only works inside a notebook: ``get_ipython()`` is ``None``
        # anywhere else, so a call on it fails.
        calls = "\n".join(_ipython_call(line, changes) for line in lines)
        return (calls, tuple(changes)) if changes and parses(calls) else (source, ())

    lines = _cell_magic(lines, changes)
    fixed = "\n".join(_fix_line(line, changes) for line in lines)
    if changes and parses(fixed):
        return fixed, tuple(changes)
    return source, ()


def _first_content(lines: list[str]) -> int | None:
    return next((i for i, line in enumerate(lines) if line.strip()), None)


def _cell_magic(lines: list[str], changes: list[str]) -> list[str]:
    """Handle a ``%%magic`` on the first line of a cell."""
    first = _first_content(lines)
    if first is None or not lines[first].lstrip().startswith("%%"):
        return lines
    header = lines[first].strip()
    name, _, rest = header[2:].partition(" ")
    body = "\n".join(lines[first + 1 :])
    kept = lines[:first]

    if name in _DROPPED_CELL_MAGICS:
        changes.append(f"cell-magic:{name}")
        return [*kept, f"{NOTE} removed: {header}", *lines[first + 1 :]]
    if name in {"bash", "sh"}:
        changes.append(f"cell-magic:{name}")
        shell = "/bin/bash" if name == "bash" else "/bin/sh"
        call = f"__import__('subprocess').run({body!r}, shell=True, executable={shell!r})"
        return [*kept, f"{NOTE} was: {header}", call]
    if name == "writefile":
        return _writefile(kept, header, rest, body, changes) or lines
    return lines


def _writefile(
    kept: list[str], header: str, rest: str, body: str, changes: list[str]
) -> list[str] | None:
    parts = rest.split()
    append = "-a" in parts
    paths = [p for p in parts if not p.startswith("-")]
    if len(paths) != 1:
        return None
    changes.append("cell-magic:writefile")
    mode = "a" if append else "w"
    return [
        *kept,
        f"{NOTE} was: {header}",
        f"with open({paths[0]!r}, {mode!r}) as _readie_file:",
        f"    _readie_file.write({body!r})",
    ]


def _ipython_call(line: str, changes: list[str]) -> str:
    """``get_ipython().run_line_magic('x', 'y')`` and friends, as the magic they stand for."""
    found = _IPYTHON_CALL.match(line)
    if found is None:
        return line
    indent, method, first = found.group("indent"), found.group("method"), found.group("first")
    if method == "system":
        return _shell_line(indent, "!" + first, changes)
    name = first.lstrip("%")
    args = found.group("second") or ""
    if method == "magic":
        name, _, args = name.partition(" ")
    changes.append(f"ipython-call:{name}")
    return _magic_line(indent, f"%{name} {args}".rstrip(), changes)


def _fix_line(line: str, changes: list[str]) -> str:
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    called = _ipython_call(line, changes)
    if called != line:
        return called

    assigned = _ASSIGNED_SHELL.match(line)
    if assigned:
        changes.append("shell:capture")
        target, command = assigned.group(2), assigned.group(3).strip()
        return f"{assigned.group(1)}{target} = {_run(command, capture=True)}"
    if stripped.startswith("!"):
        return _shell_line(indent, stripped, changes)
    if stripped.startswith("%") and not stripped.startswith("%%"):
        return _magic_line(indent, stripped, changes)
    return line


def _shell_line(indent: str, stripped: str, changes: list[str]) -> str:
    command = stripped[1:].strip()
    words = command.split()
    head = words[0] if words else ""
    if head in _INSTALL_COMMANDS or command.startswith("python -m pip"):
        changes.append("shell:install")
        return f"{indent}{NOTE} not installed: {stripped}"
    changes.append("shell:command")
    return f"{indent}{_run(command, capture=False)}"


def _run(command: str, *, capture: bool) -> str:
    literal = repr(command)
    if _BRACED.search(command):
        literal = "f" + literal
    extra = ", capture_output=True, text=True" if capture else ""
    call = f"__import__('subprocess').run({literal}, shell=True{extra})"
    return f"{call}.stdout.splitlines()" if capture else call


def _magic_line(indent: str, stripped: str, changes: list[str]) -> str:
    name, _, rest = stripped[1:].partition(" ")
    rest = rest.strip()
    if name in _INSTALLERS:
        changes.append("magic:install")
        return f"{indent}{NOTE} not installed: {stripped}"
    if name in _DROPPED:
        changes.append(f"magic:{name}")
        return f"{indent}{NOTE} removed: {stripped}"
    statement = _statement_magic(name, rest, stripped, changes)
    return f"{indent}{statement}" if statement is not None else f"{indent}{stripped}"


def _statement_magic(name: str, rest: str, stripped: str, changes: list[str]) -> str | None:
    """The Python a magic that does something stands for, or ``None`` to leave it."""
    if name in {"time", "timeit"}:
        changes.append(f"magic:{name}")
        statement = _TIMEIT_OPTIONS.sub("", rest) if name == "timeit" else rest
        return statement or f"{NOTE} removed: {stripped}"
    if name == "env":
        return _env_line(rest, stripped, changes)
    if name == "cd" and rest:
        changes.append("magic:cd")
        return f"__import__('os').chdir({rest!r})  {NOTE} was: {stripped}"
    if name == "pylab":
        changes.append("magic:pylab")
        return f"from pylab import *  {NOTE} was: {stripped}"
    return None


def _env_line(rest: str, stripped: str, changes: list[str]) -> str:
    if not rest:
        changes.append("magic:env")
        return f"{NOTE} removed: {stripped}"
    key, sep, value = rest.partition("=")
    if not sep:
        key, _, value = rest.partition(" ")
    changes.append("magic:env")
    assign = f"__import__('os').environ[{key.strip()!r}] = {value.strip()!r}"
    return f"{assign}  {NOTE} was: {stripped}"


def _with_prelude(steps: list[str]) -> list[str] | None:
    """Prepend the Kaggle prelude when the code uses Kaggle paths.

    The prelude makes ``/kaggle/working`` and ``/kaggle/input`` exist and works
    from the first, so relative ``../input`` paths and output files behave. It is
    skipped when it is already there or when it would break the first step (a
    ``__future__`` import must stay first).
    """
    if not steps or any(PRELUDE_START in step for step in steps):
        return None
    if not any(_KAGGLE_PATHS.search(step) for step in steps):
        return None
    candidate = _PRELUDE + steps[0]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(candidate, "<step>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return None
    return [candidate, *steps[1:]]


def split_prelude(step: str) -> tuple[str, str]:
    """The step's prelude block (empty when there is none) and the rest of the step."""
    start = step.find(PRELUDE_START)
    end = step.find(PRELUDE_END)
    if start < 0 or end < start:
        return "", step
    stop = end + len(PRELUDE_END)
    block = step[start:stop] + "\n"
    return block, (step[:start] + step[stop:].lstrip("\n"))


def strip_prelude(step: str) -> str:
    """The step without its prelude block, for reading what the code itself does."""
    return split_prelude(step)[1]
