"""Check that every resource an item names is real.

A resource is accepted only when the service that owns it says it exists:
PyPI for packages (or the base image, which holds them by definition), the
Hugging Face Hub for models, tokenizers and datasets, Kaggle for datasets and
models.

The cache holds only what those services said, so cpu and gpu runs share it. The
base image's package list is flavor specific (the gpu image holds packages the
cpu image does not) and is applied after the cache, never stored in it.

A lookup that fails for any other reason (network, rate limit, server error)
raises :class:`ResourceLookupError` and is never recorded as "does not exist", so
an outage cannot silently discard real work.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterable
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import Any, Protocol

from readie_pipeline.corpus.models import ResourceKind, parse_resource

#: Import names that differ from the PyPI project that provides them and are
#: common enough that guessing would waste requests.
_DISTRIBUTION_ALIASES = {
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "skimage": "scikit-image",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "umap": "umap-learn",
    "torch_geometric": "torch-geometric",
    "tensorflow_hub": "tensorflow-hub",
    "pywt": "PyWavelets",
    "bayes_opt": "bayesian-optimization",
    "iterstrat": "iterative-stratification",
    "mpl_toolkits": "matplotlib",
    "pylab": "matplotlib",
    "Bio": "biopython",
    "jwt": "PyJWT",
    "serial": "pyserial",
    "dotenv": "python-dotenv",
    "attr": "attrs",
    "OpenSSL": "pyOpenSSL",
    "magic": "python-magic",
    "fitz": "PyMuPDF",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "sknetwork": "scikit-network",
    "talib": "TA-Lib",
    "autosklearn": "auto-sklearn",
    "speech_recognition": "SpeechRecognition",
    "imblearn": "imbalanced-learn",
    "skopt": "scikit-optimize",
    "sklearn_extra": "scikit-learn-extra",
    "faiss": "faiss-cpu",
    "git": "GitPython",
    "zmq": "pyzmq",
    "psycopg2": "psycopg2-binary",
    "MySQLdb": "mysqlclient",
    "Levenshtein": "levenshtein",
}

#: Bump when the rules for deciding that a resource exists change. Answers
#: recorded under older rules that may be wrong are discarded on load, because a
#: wrong answer would otherwise be reused forever. Version 2 dropped "missing"
#: answers (the first rules rejected real packages). Version 3 dropped package
#: answers, which had been mixed with one flavor's base image list; they are
#: re-asked of PyPI alone. A resource that was found stays found, and Hub and
#: Kaggle answers do not depend on the rules, so those are kept.
RULES_VERSION = 3


class ResourceLookupError(Exception):
    """A service could not answer; the resource's existence is unknown."""


class ResourceChecker(Protocol):
    """Answers whether one resource exists."""

    def exists(self, kind: ResourceKind, value: str) -> bool:
        """Return whether the resource exists, or raise :class:`ResourceLookupError`."""
        ...


class KaggleCatalog(Protocol):
    """The part of Kaggle that existence checks need."""

    def dataset_exists(self, slug: str) -> bool:
        """Whether ``owner/slug`` is a Kaggle dataset."""
        ...

    def model_exists(self, slug: str) -> bool:
        """Whether ``owner/model`` is a Kaggle model."""
        ...


class ResourceVerifier:
    """Verify resources through a checker, remembering every definite answer."""

    def __init__(
        self,
        checker: ResourceChecker,
        cache_path: Path | None = None,
        *,
        known_packages: frozenset[str] = frozenset(),
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._checker = checker
        self._cache_path = cache_path
        self._known_packages = known_packages
        self._attempts = attempts
        self._sleep = sleep
        self._known: dict[str, bool] = self._load()

    def _load(self) -> dict[str, bool]:
        if self._cache_path is None or not self._cache_path.exists():
            return {}
        try:
            raw = json.loads(self._cache_path.read_text())
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        # A flat file is the first format, written under the first rules.
        rules = raw.get("rules", 1) if "answers" in raw else 1
        answers = raw.get("answers", {}) if "answers" in raw else raw
        if not isinstance(answers, dict):
            return {}
        kept: dict[str, bool] = {}
        for key, value in answers.items():
            if rules < 2 and not value:  # noqa: PLR2004
                continue
            if rules < RULES_VERSION and str(key).startswith("package:"):
                continue
            kept[str(key)] = bool(value)
        return kept

    def flush(self) -> None:
        """Write the cache to disk."""
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._cache_path.with_suffix(".partial")
        document = {"rules": RULES_VERSION, "answers": self._known}
        temp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        temp.replace(self._cache_path)

    def missing(self, resources: Iterable[str]) -> list[str]:
        """The resources that do not exist, in order.

        Raises:
            ResourceLookupError: A resource's existence could not be determined.
        """
        absent = [r for r in resources if not self._exists(r)]
        self.flush()
        return absent

    def _exists(self, text: str) -> bool:
        """The service's answer, then this flavor's base image as a second chance.

        Only the service's answer is cached. The image list is applied afterwards
        so that a flavor never inherits another flavor's installed packages.
        """
        kind, value = parse_resource(text)
        try:
            if text not in self._known:
                self._known[text] = self._ask(kind, value)
        except ResourceLookupError:
            # The service could not answer; a package installed in the image is
            # real regardless, anything else stays unknown.
            if self._in_image(kind, value):
                return True
            raise
        return self._known[text] or self._in_image(kind, value)

    def _in_image(self, kind: ResourceKind, value: str) -> bool:
        if kind is not ResourceKind.PACKAGE:
            return False
        return value.split(".", maxsplit=1)[0] in self._known_packages

    def _ask(self, kind: ResourceKind, value: str) -> bool:
        last: ResourceLookupError | None = None
        for attempt in range(self._attempts):
            try:
                return self._checker.exists(kind, value)
            except ResourceLookupError as exc:
                last = exc
                self._sleep(2.0**attempt)
        msg = f"could not check {kind.value}:{value}: {last}"
        raise ResourceLookupError(msg)


class LiveChecker:
    """Checks resources against PyPI, the Hugging Face Hub and Kaggle."""

    def __init__(
        self, kaggle: KaggleCatalog | None, hf_token: str | None, http: Any = None
    ) -> None:
        self._kaggle = kaggle
        self._hf_token = hf_token
        self._http = http
        self._distributions = packages_distributions()

    def exists(self, kind: ResourceKind, value: str) -> bool:
        """Dispatch to the service that owns ``kind``."""
        if kind is ResourceKind.PACKAGE:
            return self._package(value)
        if kind in (ResourceKind.HF_MODEL, ResourceKind.HF_TOKENIZER):
            return self._hub("model", value)
        if kind is ResourceKind.HF_DATASET:
            return self._hub("dataset", value)
        if self._kaggle is None:
            msg = "no Kaggle credentials configured"
            raise ResourceLookupError(msg)
        if kind is ResourceKind.KAGGLE_DATASET:
            return self._kaggle.dataset_exists(value)
        return self._kaggle.model_exists(value)

    def _package(self, dotted: str) -> bool:
        """Whether PyPI has a project this import name could come from.

        An import name is often not its PyPI project name (``pywt`` is
        ``PyWavelets``), so the aliases and the distributions installed here are
        tried alongside the name itself. A package PyPI does not recognize may
        still be installed in the image the code runs in; the verifier consults
        that list after this answer.
        """
        top = dotted.split(".", maxsplit=1)[0]
        if top in sys.stdlib_module_names:
            return True
        names = [
            *self._distributions.get(top, []),
            _DISTRIBUTION_ALIASES.get(top, top),
            top.replace("_", "-"),
        ]
        return any(self._pypi(name) for name in dict.fromkeys(names))

    def _pypi(self, name: str) -> bool:
        import httpx  # noqa: PLC0415 - optional extra

        client = self._http or httpx
        try:
            response = client.get(f"https://pypi.org/pypi/{name}/json", timeout=20.0)
        except httpx.HTTPError as exc:
            raise ResourceLookupError(str(exc)) from exc
        if response.status_code == 200:  # noqa: PLR2004
            return True
        if response.status_code == 404:  # noqa: PLR2004
            return False
        msg = f"PyPI answered {response.status_code} for {name}"
        raise ResourceLookupError(msg)

    def _hub(self, repo_type: str, repo_id: str) -> bool:
        from huggingface_hub import HfApi  # noqa: PLC0415 - optional extra
        from huggingface_hub.errors import (  # noqa: PLC0415
            GatedRepoError,
            HfHubHTTPError,
            RepositoryNotFoundError,
        )

        api = HfApi(token=self._hf_token)
        try:
            if repo_type == "dataset":
                api.dataset_info(repo_id)
            else:
                api.model_info(repo_id)
        except GatedRepoError:
            return True
        except RepositoryNotFoundError:
            return False
        except HfHubHTTPError as exc:
            raise ResourceLookupError(str(exc)) from exc
        except OSError as exc:
            raise ResourceLookupError(str(exc)) from exc
        return True
