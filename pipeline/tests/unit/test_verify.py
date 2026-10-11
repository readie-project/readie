import json
from pathlib import Path

import pytest

from readie_pipeline.corpus.models import ResourceKind
from readie_pipeline.corpus.verify import LiveChecker, ResourceLookupError, ResourceVerifier


class Checker:
    def __init__(self, real: set[str], flaky: int = 0):
        self.real = real
        self.flaky = flaky
        self.calls: list[str] = []

    def exists(self, kind, value):
        self.calls.append(f"{kind.value}:{value}")
        if self.flaky:
            self.flaky -= 1
            raise ResourceLookupError("503")
        return f"{kind.value}:{value}" in self.real


def verifier(checker, path=None, attempts=3):
    return ResourceVerifier(checker, path, attempts=attempts, sleep=lambda _: None)


def test_only_resources_that_do_not_exist_are_reported():
    v = verifier(Checker({"package:numpy"}))
    assert v.missing(["package:numpy", "hf_model:org/fake"]) == ["hf_model:org/fake"]


def test_an_answer_is_cached_and_not_asked_twice():
    checker = Checker({"package:numpy"})
    v = verifier(checker)
    v.missing(["package:numpy", "hf_model:org/fake"])
    v.missing(["package:numpy", "hf_model:org/fake"])
    assert checker.calls == ["package:numpy", "hf_model:org/fake"]


def test_the_cache_survives_between_runs(tmp_path: Path):
    path = tmp_path / "cache" / "verified.json"
    verifier(Checker({"package:numpy"}), path).missing(["package:numpy"])

    again = Checker(set())
    assert verifier(again, path).missing(["package:numpy"]) == []
    assert again.calls == []
    assert json.loads(path.read_text())["answers"] == {"package:numpy": True}


def test_a_transient_failure_is_retried():
    checker = Checker({"package:numpy"}, flaky=2)
    assert verifier(checker).missing(["package:numpy"]) == []


def test_a_persistent_failure_is_not_recorded_as_a_missing_resource(tmp_path: Path):
    # An outage must never turn a real package into a cached "does not exist".
    path = tmp_path / "verified.json"
    v = verifier(Checker({"package:numpy"}, flaky=99), path)
    with pytest.raises(ResourceLookupError, match="package:numpy"):
        v.missing(["package:numpy"])
    assert not path.exists() or "package:numpy" not in json.loads(path.read_text())["answers"]


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeHttp:
    def __init__(self, known: set[str]):
        self.known = known
        self.urls: list[str] = []

    def get(self, url, timeout):
        self.urls.append(url)
        name = url.split("/pypi/")[1].split("/json")[0]
        return FakeResponse(200 if name in self.known else 404)


def test_the_standard_library_needs_no_lookup():
    http = FakeHttp(set())
    assert LiveChecker(None, None, http).exists(ResourceKind.PACKAGE, "os.path")
    assert http.urls == []


def test_an_import_name_is_mapped_to_the_project_that_provides_it():
    http = FakeHttp({"scikit-learn"})
    assert LiveChecker(None, None, http).exists(ResourceKind.PACKAGE, "sklearn.svm.LinearSVC")


def test_a_package_pypi_does_not_know_is_missing():
    assert not LiveChecker(None, None, FakeHttp(set())).exists(
        ResourceKind.PACKAGE, "totally_made_up_pkg"
    )


def test_an_unexpected_pypi_status_is_a_lookup_failure_not_a_missing_package():
    class Down(FakeHttp):
        def get(self, url, timeout):
            return FakeResponse(503)

    with pytest.raises(ResourceLookupError, match="503"):
        LiveChecker(None, None, Down(set())).exists(ResourceKind.PACKAGE, "numpy_x")


def test_a_kaggle_resource_without_credentials_cannot_be_checked():
    with pytest.raises(ResourceLookupError, match="Kaggle"):
        LiveChecker(None, None).exists(ResourceKind.KAGGLE_DATASET, "o/d")


class Catalog:
    def dataset_exists(self, slug):
        return slug == "o/real"

    def model_exists(self, slug):
        return slug == "o/model"


def test_kaggle_resources_go_to_the_kaggle_catalog():
    checker = LiveChecker(Catalog(), None)
    assert checker.exists(ResourceKind.KAGGLE_DATASET, "o/real")
    assert not checker.exists(ResourceKind.KAGGLE_DATASET, "o/fake")
    assert checker.exists(ResourceKind.KAGGLE_MODEL, "o/model")


# ---------------------------------------------------------------------------
# Recall: real packages must not be rejected for having an unfamiliar import name
# ---------------------------------------------------------------------------
def image_verifier(checker, known, path=None):
    return ResourceVerifier(checker, path, known_packages=frozenset(known), sleep=lambda _: None)


def test_pypi_is_asked_first_and_the_base_image_list_only_when_it_has_no_answer():
    # Seen live: mpl_toolkits, pylab, pywt and bayes_opt were rejected because
    # their import names are not their PyPI project names and the pipeline's own
    # venv does not have them installed. The base image's list rescues them, but
    # only after PyPI has been asked.
    checker = Checker(set())
    v = image_verifier(checker, {"mpl_toolkits", "pywt"})

    assert v.missing(["package:mpl_toolkits.mplot3d.Axes3D", "package:pywt"]) == []
    assert checker.calls == ["package:mpl_toolkits.mplot3d.Axes3D", "package:pywt"]


def test_a_package_pypi_knows_is_accepted_whatever_the_base_image_holds():
    checker = Checker({"package:requests"})
    assert image_verifier(checker, set()).missing(["package:requests"]) == []


def test_an_unreachable_service_falls_back_to_the_base_image_list():
    checker = Checker(set(), flaky=99)
    assert image_verifier(checker, {"pywt"}).missing(["package:pywt"]) == []


def test_an_unreachable_service_is_still_a_lookup_failure_for_an_unknown_package():
    with pytest.raises(ResourceLookupError):
        image_verifier(Checker(set(), flaky=99), {"pywt"}).missing(["package:ghost_pkg"])


def test_the_base_image_list_does_not_apply_to_hub_or_kaggle_resources():
    v = image_verifier(Checker(set()), {"fake"})
    assert v.missing(["hf_model:fake", "kaggle_dataset:fake/fake"]) == [
        "hf_model:fake",
        "kaggle_dataset:fake/fake",
    ]


# -- one cache, two flavors ---------------------------------------------------
def test_the_cache_records_what_the_service_said_not_what_the_image_added(tmp_path: Path):
    path = tmp_path / "verified.json"
    image_verifier(Checker(set()), {"cudf"}, path).missing(["package:cudf"])

    # The cpu image has no cudf, so a gpu run must not inherit an "exists" for it.
    assert json.loads(path.read_text())["answers"] == {"package:cudf": False}


def test_two_flavors_share_the_cache_but_not_each_others_images(tmp_path: Path):
    path = tmp_path / "verified.json"
    gpu = image_verifier(Checker({"hf_model:org/m"}), {"cudf"}, path)
    assert gpu.missing(["package:cudf", "hf_model:org/m"]) == []

    cpu_checker = Checker(set())
    cpu = image_verifier(cpu_checker, set(), path)

    # The Hub answer is shared and needs no second lookup.
    assert cpu.missing(["hf_model:org/m"]) == []
    # The gpu image's package is not real to the cpu image.
    assert cpu.missing(["package:cudf"]) == ["package:cudf"]
    assert "hf_model:org/m" not in cpu_checker.calls


@pytest.mark.parametrize(
    ("import_name", "project"),
    [
        ("pywt", "PyWavelets"),
        ("bayes_opt", "bayesian-optimization"),
        ("iterstrat", "iterative-stratification"),
        ("mpl_toolkits", "matplotlib"),
        ("pylab", "matplotlib"),
        # Seen live in the 1000-item run: real packages rejected for their import name.
        ("sknetwork", "scikit-network"),
        ("talib", "TA-Lib"),
        ("autosklearn", "auto-sklearn"),
        ("speech_recognition", "SpeechRecognition"),
        ("imblearn", "imbalanced-learn"),
    ],
)
def test_common_imports_whose_name_differs_from_their_project_are_found(import_name, project):
    assert LiveChecker(None, None, FakeHttp({project})).exists(ResourceKind.PACKAGE, import_name)


def test_missing_answers_from_the_first_rules_are_dropped_but_found_ones_are_kept(tmp_path: Path):
    # A flat file is the first cache format. Its "missing" answers came from rules
    # that wrongly rejected real resources, so they must be asked again.
    path = tmp_path / "verified.json"
    path.write_text(json.dumps({"hf_model:org/a": True, "hf_model:org/b": False}))

    checker = Checker({"hf_model:org/b"})
    v = verifier(checker, path)

    assert v.missing(["hf_model:org/a", "hf_model:org/b"]) == []
    assert checker.calls == ["hf_model:org/b"]


def test_package_answers_from_older_rules_are_re_asked_because_they_mixed_in_one_image(
    tmp_path: Path,
):
    # Version 2 stored answers that had the cpu image's package list folded in,
    # so sharing them with a gpu run would leak one image into the other.
    path = tmp_path / "verified.json"
    path.write_text(
        json.dumps(
            {
                "rules": 2,
                "answers": {
                    "package:learntools": True,
                    "package:ghost": False,
                    "hf_model:org/a": True,
                    "kaggle_dataset:o/d": False,
                },
            }
        )
    )
    checker = Checker({"package:learntools"})
    v = verifier(checker, path)

    assert v.missing(["hf_model:org/a", "kaggle_dataset:o/d", "package:learntools"]) == [
        "kaggle_dataset:o/d"
    ]
    # Hub and Kaggle answers are kept as they were; only the package was re-asked.
    assert checker.calls == ["package:learntools"]


def test_answers_written_under_the_current_rules_are_all_kept(tmp_path: Path):
    path = tmp_path / "verified.json"
    first = verifier(Checker({"package:numpy"}), path)
    first.missing(["package:numpy", "hf_model:org/fake"])

    again = Checker(set())
    assert verifier(again, path).missing(["hf_model:org/fake"]) == ["hf_model:org/fake"]
    assert again.calls == []
