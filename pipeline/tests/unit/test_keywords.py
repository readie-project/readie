import json
import random
from pathlib import Path

from readie_pipeline.corpus.keywords import PER_EXPANSION, KeywordBank

SEEDS: dict[str, dict[str, tuple[str, ...]]] = {
    "kaggle": {"Data Science": ("data science", "statistics")},
    "huggingface": {"Data Science": ("sklearn",)},
}


class Chat:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, system, user):
        self.prompts.append(user)
        return self.replies.pop(0) if self.replies else '{"terms": []}'


def bank(chat=None, path=None):
    return KeywordBank(SEEDS, chat, path)


def test_seeds_come_first_and_learned_phrases_follow_without_repeats():
    b = bank(Chat('{"terms": ["churn xgboost", "Data Science"]}'))
    b.broaden("kaggle", "Data Science", "cpu")
    assert b.terms("kaggle", "Data Science", "cpu") == (
        "data science",
        "statistics",
        "churn xgboost",
    )


def test_a_phrase_not_yet_used_is_preferred_over_one_already_used():
    b = bank()
    rng = random.Random(0)
    picked = {b.pick("kaggle", "Data Science", "cpu", rng) for _ in range(2)}
    assert picked == {"data science", "statistics"}


def test_when_every_phrase_has_been_used_it_picks_again_rather_than_failing():
    b = bank()
    rng = random.Random(0)
    for _ in range(5):
        assert b.pick("kaggle", "Data Science", "cpu", rng) in {"data science", "statistics"}


def test_unusable_phrases_are_dropped_and_the_rest_are_cleaned():
    reply = json.dumps(
        {
            "terms": [
                "  House  Prices ",
                "",
                "x",
                "a" * 41,
                7,
                None,
                'it\'s "quoted"',
                "statistics",
                "ok term",
            ]
        }
    )
    b = bank(Chat(reply))
    added = b.broaden("kaggle", "Data Science", "cpu")
    assert b.terms("kaggle", "Data Science", "cpu")[2:] == ("house prices", "ok term")
    assert added == 2


def test_kaggle_phrases_of_more_than_two_words_are_dropped_but_other_sites_keep_them():
    # Pinned: Kaggle needs every word to match, so "lstm forecasting pandas" found
    # almost nothing and the model-grown phrases were mostly that long.
    reply = '{"terms": ["arima", "seasonal decomposition", "lstm forecasting pandas"]}'
    kaggle = bank(Chat(reply))
    kaggle.broaden("kaggle", "Data Science", "cpu")
    assert kaggle.terms("kaggle", "Data Science", "cpu")[2:] == ("arima", "seasonal decomposition")

    hub = KeywordBank({"huggingface": {"Data Science": ()}}, Chat(reply))
    hub.broaden("huggingface", "Data Science", "cpu")
    assert "lstm forecasting pandas" in hub.terms("huggingface", "Data Science", "cpu")


def test_a_reply_that_repeats_only_known_phrases_adds_nothing():
    assert (
        bank(Chat('{"terms": ["data science", "STATISTICS"]}')).broaden(
            "kaggle", "Data Science", "cpu"
        )
        == 0
    )


def test_no_model_and_a_failed_or_malformed_reply_all_add_nothing_and_do_not_raise():
    assert bank().broaden("kaggle", "Data Science", "cpu") == 0
    assert bank(Chat("not json at all")).broaden("kaggle", "Data Science", "cpu") == 0
    assert bank(Chat('{"terms": "a string"}')).broaden("kaggle", "Data Science", "cpu") == 0
    assert bank(Chat('{"other": []}')).broaden("kaggle", "Data Science", "cpu") == 0


def test_learned_phrases_are_saved_and_a_later_run_starts_from_them(tmp_path: Path):
    path = tmp_path / "cache" / "keywords.json"
    bank(Chat('{"terms": ["learned one", "learned two"]}'), path).broaden(
        "kaggle", "Data Science", "cpu"
    )

    later = bank(None, path)
    assert later.terms("kaggle", "Data Science", "cpu")[-2:] == ("learned one", "learned two")
    assert not path.with_suffix(".partial").exists()


def test_phrases_learned_for_one_flavor_platform_or_domain_are_not_used_for_another():
    b = bank(Chat('{"terms": ["only cpu kaggle"]}'))
    b.broaden("kaggle", "Data Science", "cpu")

    assert "only cpu kaggle" not in b.terms("kaggle", "Data Science", "gpu")
    assert "only cpu kaggle" not in b.terms("huggingface", "Data Science", "cpu")
    assert "only cpu kaggle" not in b.terms("kaggle", "Graph Processing", "cpu")


def test_the_prompt_says_what_the_platforms_search_actually_matches():
    chat = Chat()
    b = bank(chat)
    b.broaden("kaggle", "Data Science", "cpu", ["titanic-survival", "house-prices"])
    b.broaden("huggingface", "Data Science", "gpu")
    kaggle, hub = chat.prompts

    assert "notebook titles" in kaggle
    assert "without a GPU" in kaggle
    assert "titanic-survival, house-prices" in kaggle
    assert "data science, statistics" in kaggle  # what was already tried
    # Pinned: Hub search matches repository names only, so long phrases find nothing.
    assert "NAMES only" in hub
    assert "CUDA GPU" in hub
    assert f"{PER_EXPANSION} new phrases" in hub
