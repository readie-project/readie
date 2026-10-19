from readie_pipeline.corpus.runnable import (
    NOTE,
    PRELUDE_END,
    PRELUDE_START,
    clean_cell,
    clean_steps,
    parses,
    strip_prelude,
)


def fixed(source: str) -> str:
    return clean_cell(source)[0]


def test_a_cell_that_already_parses_is_never_touched():
    # A line starting with "!" inside a string is data, not a shell escape.
    source = 'text = """\n!pip install not-a-command\n%matplotlib inline\n"""\nprint(text)'
    assert clean_cell(source) == (source, ())


def test_display_only_magics_become_comments_and_keep_their_text():
    out = fixed("%matplotlib inline\nimport numpy as np\n%load_ext autoreload\n%autoreload 2")
    assert out.splitlines()[0] == f"{NOTE} removed: %matplotlib inline"
    assert "import numpy as np" in out
    assert parses(out)


def test_pip_installs_are_commented_out_and_never_run():
    out = fixed("!pip install lightgbm xgboost\n%pip install -q seaborn\nimport lightgbm")
    assert f"{NOTE} not installed: !pip install lightgbm xgboost" in out
    assert f"{NOTE} not installed: %pip install -q seaborn" in out
    assert parses(out)


def test_other_shell_lines_become_subprocess_calls():
    out = fixed("import os\n!ls -la data\n")
    assert "__import__('subprocess').run('ls -la data', shell=True)" in out
    assert parses(out)


def test_a_shell_line_that_captures_output_gives_the_lines():
    out = fixed("files = !ls /tmp")
    assert out == (
        "files = __import__('subprocess').run('ls /tmp', shell=True, "
        "capture_output=True, text=True).stdout.splitlines()"
    )


def test_a_shell_line_with_braced_variables_is_interpolated_like_ipython():
    out = fixed("path = 'a'\n!echo {path}")
    assert "run(f'echo {path}', shell=True)" in out
    assert parses(out)


def test_time_magics_keep_the_statement():
    assert fixed("%time x = sum(range(10))") == "x = sum(range(10))"
    assert fixed("%timeit -n 5 -r 2 sum(range(10))") == "sum(range(10))"
    out = fixed("%%time\nx = 1\ny = 2")
    assert out.splitlines()[1:] == ["x = 1", "y = 2"]
    assert out.startswith(f"{NOTE} removed: %%time")


def test_env_and_cd_magics_become_python():
    out = fixed("%env OMP_NUM_THREADS=4\n%cd /tmp")
    assert "environ['OMP_NUM_THREADS'] = '4'" in out
    assert "chdir('/tmp')" in out
    assert parses(out)


def test_bash_and_writefile_cell_magics_become_python():
    bash = fixed("%%bash\necho hi\nls")
    assert "run('echo hi\\nls', shell=True, executable='/bin/bash')" in bash
    write = fixed("%%writefile helper.py\ndef f():\n    return 1\n")
    assert "with open('helper.py', 'w') as _readie_file:" in write
    assert parses(write)


def test_a_cell_whose_fix_would_not_parse_is_left_exactly_as_it_was():
    source = "%run script.py\nx = (1 +"
    assert clean_cell(source) == (source, ())


def test_an_unknown_magic_leaves_the_cell_alone():
    source = "%some_unknown_magic arg\nx = 1"
    assert clean_cell(source) == (source, ())


def test_cleaning_is_idempotent():
    steps = (
        "%matplotlib inline\n!pip install x\nimport pandas as pd\n"
        "df = pd.read_csv('../input/a.csv')",
    )
    once = clean_steps(steps)
    twice = clean_steps(once.code)
    assert twice.code == once.code
    assert twice.changes == ()


def test_kaggle_paths_get_a_prelude_that_creates_the_folders_and_moves_there():
    out = clean_steps(("import pandas as pd\ndf = pd.read_csv('../input/titanic/train.csv')",))
    first = out.code[0]
    assert first.startswith(PRELUDE_START)
    assert "os.makedirs('/kaggle/working', exist_ok=True)" in first
    assert "os.chdir('/kaggle/working')" in first
    assert "prelude" in out.changes


def test_code_without_kaggle_paths_gets_no_prelude():
    assert clean_steps(("import pandas as pd",)).code == ("import pandas as pd",)


def test_a_future_import_keeps_the_prelude_out_so_the_step_still_compiles():
    steps = ("from __future__ import annotations\nx = open('/kaggle/input/a.csv')",)
    assert clean_steps(steps).code == steps


def test_the_prelude_can_be_stripped_to_read_what_the_code_imports():
    step = clean_steps(("import pandas\nx = '/kaggle/input/a'",)).code[0]
    stripped = strip_prelude(step)
    assert PRELUDE_END not in stripped
    assert "import os" not in stripped
    assert stripped.startswith("import pandas")


def test_steps_are_cleaned_independently():
    out = clean_steps(("%matplotlib inline\nimport numpy", "x = 1"))
    assert out.code[1] == "x = 1"
    assert out.changes == ("magic:matplotlib",)


# -- code that parses but only works inside a notebook --------------------------------
def test_get_ipython_calls_that_run_a_display_magic_are_commented_out():
    # Seen in the trial: `get_ipython()` is None outside a notebook, so this raised
    # "'NoneType' object has no attribute 'run_line_magic'" in a cell that parses.
    source = (
        "from IPython import get_ipython\n"
        "get_ipython().run_line_magic('matplotlib', 'inline')\nx = 1"
    )
    out, changes = clean_cell(source)
    assert f"{NOTE} removed: %matplotlib inline" in out
    assert "get_ipython().run_line_magic" not in out
    assert out.endswith("x = 1")
    assert "ipython-call:matplotlib" in changes
    assert parses(out)


def test_the_old_magic_spelling_and_a_leading_percent_are_understood():
    out = fixed(
        "get_ipython().magic('matplotlib inline')\nget_ipython().magic(\"%load_ext autoreload\")"
    )
    assert out.count(f"{NOTE} removed:") == 2
    assert "get_ipython" not in out


def test_a_get_ipython_system_call_becomes_a_subprocess_call():
    out = fixed("get_ipython().system('ls -la')")
    assert out == "__import__('subprocess').run('ls -la', shell=True)"


def test_get_ipython_pip_installs_are_not_run():
    out = fixed("get_ipython().system('pip install seaborn')")
    assert out == f"{NOTE} not installed: !pip install seaborn"


def test_an_indented_call_keeps_its_indent_so_the_block_still_parses():
    out = fixed("if True:\n    get_ipython().run_line_magic('matplotlib', 'inline')\n    y = 2")
    assert parses(out)
    assert "    y = 2" in out


def test_other_get_ipython_uses_are_left_alone():
    for source in (
        "ip = get_ipython()\nif ip is not None:\n    ip.run_line_magic('matplotlib', 'inline')",
        "get_ipython().run_cell(code)",
        "shell = get_ipython().__class__.__name__",
    ):
        assert clean_cell(source) == (source, ())


def test_the_get_ipython_fix_is_idempotent():
    once = clean_steps(("get_ipython().run_line_magic('matplotlib', 'inline')\nx = 1",))
    assert clean_steps(once.code).code == once.code
