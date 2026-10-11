from readie_pipeline.corpus.dependencies import (
    MISSING_INPUT,
    NETWORK,
    SECRET,
    UNPARSABLE,
    missing_inputs,
    normalise_path,
    scan,
)


def kinds(code):
    return {f.kind for f in scan(code)}


def test_a_kaggle_input_path_is_a_missing_input_in_either_spelling():
    assert missing_inputs(["pd.read_csv('../input/titanic/train.csv')"]) == {
        0: ["../input/titanic/train.csv"]
    }
    assert missing_inputs(["open('/kaggle/input/a/b.txt')"]) == {0: ["/kaggle/input/a/b.txt"]}


def test_a_directory_listing_of_the_input_folder_is_flagged_too():
    assert missing_inputs(["os.listdir('/kaggle/input')"]) == {0: ["/kaggle/input"]}


def test_a_relative_data_file_that_nothing_creates_is_a_missing_input():
    assert missing_inputs(["df = pd.read_csv('sales.csv')"]) == {0: ["sales.csv"]}


def test_a_file_the_item_writes_first_is_not_missing():
    code = ["df.to_csv('out.csv')", "pd.read_csv('out.csv')"]
    assert missing_inputs(code) == {}


def test_a_standin_written_at_the_expected_path_covers_the_read_in_a_later_cell():
    code = [
        "os.makedirs('/kaggle/input/titanic', exist_ok=True)\n"
        "df.to_csv('/kaggle/input/titanic/train.csv')",
        "pd.read_csv('../input/titanic/train.csv')",
        "os.listdir('/kaggle/input')",
    ]
    assert missing_inputs(code) == {}


def test_a_written_file_under_a_folder_covers_reading_the_folder():
    code = ["open('/kaggle/input/d/a.txt', 'w').write('x')", "os.listdir('/kaggle/input/d')"]
    assert missing_inputs(code) == {}


def test_colab_and_drive_paths_are_missing_inputs():
    assert missing_inputs(["pd.read_csv('/content/drive/MyDrive/x.csv')"]) == {
        0: ["/content/drive/MyDrive/x.csv"]
    }


def test_urls_and_system_paths_are_not_missing_inputs():
    code = [
        "pd.read_csv('https://example.com/a.csv')",
        "open('/etc/hostname')",
        "np.load('/tmp/x.npy')",
    ]
    assert missing_inputs(code) == {}


def test_a_non_data_literal_read_is_ignored():
    assert missing_inputs(["open('config')", "print('hello world')"]) == {}


def test_the_cells_that_read_are_reported_by_index():
    code = ["import pandas", "x = 1", "pd.read_csv('train.csv')", "pd.read_csv('test.csv')"]
    assert missing_inputs(code) == {2: ["train.csv"], 3: ["test.csv"]}


def test_network_secrets_and_unparsable_cells_are_reported_but_are_not_missing_inputs():
    code = [
        "AutoModel.from_pretrained('bert-base-uncased')",
        "key = os.getenv('TOKEN')",
        "def broken(:",
    ]
    assert kinds(code) == {NETWORK, SECRET, UNPARSABLE}
    assert missing_inputs(code) == {}
    assert MISSING_INPUT not in kinds(code)


def test_paths_are_compared_after_normalising_the_kaggle_spellings():
    assert normalise_path("../input/a/b.csv") == "/kaggle/input/a/b.csv"
    assert normalise_path("./../input/x") == "/kaggle/input/x"
    assert normalise_path("data\\a.csv") == "data/a.csv"


def test_creating_a_folder_does_not_provide_the_files_in_it():
    code = [
        "os.makedirs('/kaggle/input/titanic', exist_ok=True)",
        "pd.read_csv('../input/titanic/train.csv')",
    ]
    assert missing_inputs(code) == {1: ["../input/titanic/train.csv"]}


def test_the_prelude_block_is_not_read_as_code_that_depends_on_kaggle_input():
    from readie_pipeline.corpus.runnable import clean_steps

    code = clean_steps(("import pandas as pd\nprint(1)\nx = '/kaggle/working/a'",)).code
    assert code[0].startswith("# [readie:prelude]")
    assert missing_inputs(code) == {}
