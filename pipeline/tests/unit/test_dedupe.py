from readie_pipeline.corpus.dedupe import DuplicateIndex, fingerprint

BASE = (
    "import pandas as pd\n"
    "df = pd.read_csv('a.csv')\n"
    "df['total'] = df['price'] * df['qty']\n"
    "summary = df.groupby('region')['total'].sum()\n"
    "print(summary.sort_values(ascending=False).head(10))\n"
)


def test_formatting_and_comments_do_not_change_a_fingerprint():
    reformatted = BASE.replace("df['total'] = ", "df['total']=  ") + "# trailing note\n"
    assert fingerprint((BASE,)) == fingerprint((reformatted,))


def test_the_same_origin_is_a_duplicate_even_if_the_code_differs():
    index = DuplicateIndex()
    index.add(("x = 1",), "https://example/a")
    assert index.is_duplicate(("y = 2",), "https://example/a")


def test_a_reformatted_copy_is_a_duplicate():
    index = DuplicateIndex()
    index.add((BASE,))
    assert index.is_duplicate((BASE.replace("print(", "print( "),))


def test_a_lightly_edited_fork_is_a_near_duplicate():
    long = "\n".join(f"df['c{i}'] = df['a'] + {i}" for i in range(60))
    index = DuplicateIndex()
    index.add((long,))
    assert index.is_duplicate((long + "\ndf['extra'] = 0",))


def test_unrelated_code_is_not_a_duplicate():
    index = DuplicateIndex()
    index.add((BASE,))
    other = "import networkx as nx\ng = nx.karate_club_graph()\nprint(nx.pagerank(g))\n"
    assert not index.is_duplicate((other,))


def test_a_notebook_is_not_a_duplicate_of_one_of_its_cells():
    index = DuplicateIndex()
    index.add((BASE,))
    assert not index.is_duplicate((BASE, "print('second cell')\nx = [i for i in range(10)]"))


def test_fingerprinting_code_with_invalid_escape_sequences_emits_no_warning():
    # pytest runs with filterwarnings=error. The duplicate check used to call
    # ast.parse itself and leaked a SyntaxWarning for every "\\d" in fetched code.
    assert fingerprint(("import re\nre.compile('\\d+\\.\\d')",)) == fingerprint(
        ("import re\nre.compile('\\d+\\.\\d')  # same",)
    )
