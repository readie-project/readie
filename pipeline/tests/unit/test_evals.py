import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from readie_pipeline.corpus.models import CorpusItem, Source
from readie_pipeline.evals.client import EvalError, HttpEvalServer, RunOptions, request_body
from readie_pipeline.evals.report import bucket, format_report, load_results, summarise_items
from readie_pipeline.evals.run import (
    Selection,
    classify,
    item_id,
    load_progress,
    run_evaluation,
    select,
)

DOMAIN = "Data Science"


def item(
    code: str = "x = 1", source: Source = Source.AGENT_SHORT, domain: str = DOMAIN, *more: str
):
    return CorpusItem((code, *more), domain, source, ("package:pandas",), ref=f"u/{code[:8]}")


def lane(total: float, *, ok: bool = True, error_type: str = "", steps: int = 1) -> dict[str, Any]:
    each = total / steps
    return {
        "session_id": None,
        "ok": ok,
        "total_s": total,
        "steps": [
            {"index": i, "ok": ok, "duration_s": each, "error": "boom" if not ok else "",
             "error_type": error_type, "output": "", "truncated": False}
            for i in range(steps)
        ],
    }  # fmt: skip


class Server:
    """Answers every item with the lanes it is given, and records the requests."""

    def __init__(self, *responses: dict[str, Any] | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[list[str], RunOptions]] = []

    def run(self, steps: list[str], options: RunOptions) -> dict[str, Any]:
        self.requests.append((steps, options))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def ok_response(opt: float = 1.0, cold: float = 3.0) -> dict[str, Any]:
    return {"session_id": None, "lanes": {"optimized": lane(opt), "cold": lane(cold)}}


def run(items, server, tmp_path: Path, **kw: Any):
    return run_evaluation(
        items, server, RunOptions(), tmp_path / "r.jsonl", on_line=lambda _: None,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC), **kw,
    )  # fmt: skip


def rows(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (tmp_path / "r.jsonl").read_text().splitlines()]


# -- the request -----------------------------------------------------------------
def test_the_request_carries_the_steps_and_the_options_for_the_whole_item() -> None:
    body = request_body(["a", "b"], RunOptions(memory="8Gi", timeout_s=300.0))
    assert body == {
        "steps": ["a", "b"], "memory": "8Gi", "max_memory": "8Gi", "timeout_s": 300.0,
        "total_timeout_s": 300.0, "packages": [], "continue_on_error": True,
    }  # fmt: skip


def test_the_defaults_are_8gib_five_minutes_and_nothing_installed() -> None:
    options = RunOptions()
    assert (options.memory, options.timeout_s, options.packages) == ("8Gi", 300.0, ())


# -- the HTTP client --------------------------------------------------------------
def http_server(handler, **kw: Any) -> HttpEvalServer:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpEvalServer("http://eval:8081/", "tok", client=client, sleep=lambda _: None, **kw)


def test_the_client_posts_to_the_eval_route_with_the_bearer_token() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_response())

    result = http_server(handler).run(["x = 1"], RunOptions())
    assert seen["url"] == "http://eval:8081/eval/run"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"]["steps"] == ["x = 1"]
    assert set(result["lanes"]) == {"optimized", "cold"}


def test_a_busy_server_is_retried_then_succeeds() -> None:
    answers = iter([503, 503, 200])

    def handler(_request: httpx.Request) -> httpx.Response:
        status = next(answers)
        return httpx.Response(status, json=ok_response() if status == 200 else {"detail": "busy"})

    assert http_server(handler).run(["x"], RunOptions())["lanes"]


@pytest.mark.parametrize(
    ("status", "text"), [(401, "wrong token"), (422, "bad memory"), (500, "oops")]
)
def test_other_http_errors_are_reported_with_the_servers_words(status: int, text: str) -> None:
    server = http_server(lambda _r: httpx.Response(status, text=text))
    with pytest.raises(EvalError, match=f"{status}.*{text}"):
        server.run(["x"], RunOptions())


def test_a_server_that_is_always_busy_gives_up() -> None:
    server = http_server(lambda _r: httpx.Response(503, text="busy"), retries=3)
    with pytest.raises(EvalError, match="503"):
        server.run(["x"], RunOptions())


def test_a_connection_failure_is_an_eval_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(EvalError, match="could not reach"):
        http_server(handler).run(["x"], RunOptions())


def test_an_answer_that_is_not_the_expected_json_is_an_eval_error() -> None:
    with pytest.raises(EvalError, match="no lanes"):
        http_server(lambda _r: httpx.Response(200, json={"nope": 1})).run(["x"], RunOptions())
    with pytest.raises(EvalError, match="not JSON"):
        http_server(lambda _r: httpx.Response(200, text="<html>")).run(["x"], RunOptions())


# -- running ---------------------------------------------------------------------
def test_every_item_is_run_once_per_repetition_and_written_as_it_arrives(tmp_path: Path) -> None:
    server = Server(ok_response())
    report = run([item("a = 1"), item("b = 2")], server, tmp_path, repetitions=2)

    assert report.runs == 4
    assert [(r["repetition"], r["outcome"]) for r in rows(tmp_path)] == [(1, "ok"), (2, "ok")] * 2
    assert len(server.requests) == 4


def test_a_multi_step_item_is_sent_as_one_request_with_all_its_steps(tmp_path: Path) -> None:
    server = Server(ok_response())
    run(
        [item("a = 1", Source.KAGGLE_NOTEBOOK, DOMAIN, "b = 2", "c = 3")],
        server,
        tmp_path,
        repetitions=1,
    )
    assert server.requests[0][0] == ["a = 1", "b = 2", "c = 3"]
    assert rows(tmp_path)[0]["steps"] == 3


def test_a_row_records_what_was_run_and_both_lanes(tmp_path: Path) -> None:
    run([item("a = 1")], Server(ok_response(1.5, 4.5)), tmp_path, repetitions=1)
    (row,) = rows(tmp_path)

    assert row["source"] == "agent_short"
    assert row["domain"] == DOMAIN
    assert row["options"] == {"memory": "8Gi", "timeout_s": 300.0}
    assert row["optimized"]["total_s"] == 1.5
    assert row["cold"]["total_s"] == 4.5
    assert row["at"] == "2026-01-01T00:00:00+00:00"


def lost_lane() -> dict[str, Any]:
    response = lane(5, ok=False, error_type="RemoteTimeoutError")
    response["steps"][0]["error"] = (
        "RemoteTimeoutError: DEADLINE_EXCEEDED from localhost:50051: "
        "the worker could not complete the execution"
    )
    return response


def test_a_sandbox_that_died_only_in_the_optimized_start_is_restore_lost() -> None:
    # Seen in the trial: a sandbox restored from a checkpoint crashed after 5 s while
    # the cold start of the same code finished. Readie reports both as a deadline error.
    assert classify({"optimized": lost_lane(), "cold": lane(30)}) == "restore_lost"


def test_restore_lost_does_not_need_the_cold_start_to_have_succeeded() -> None:
    # Only the optimized start lost its sandbox; the cold start failed for its own reasons.
    cold = lane(3, ok=False, error_type="RemoteExecutionError")
    assert classify({"optimized": lost_lane(), "cold": cold}) == "restore_lost"


def test_both_starts_losing_a_sandbox_is_plain_lost() -> None:
    assert classify({"optimized": lost_lane(), "cold": lost_lane()}) == "lost"


def test_only_the_cold_start_losing_a_sandbox_is_cold_start_lost() -> None:
    assert classify({"optimized": lane(1), "cold": lost_lane()}) == "cold_start_lost"


def test_a_lost_optimized_sandbox_is_restore_lost_even_if_the_cold_start_timed_out() -> None:
    # Pinned: all 15 items first reported as timeouts had lost the optimized sandbox
    # in seconds while the cold start ran out of time, so the restore was the problem
    # and the cold start's timeout only says the code is slow.
    real = lane(120, ok=False, error_type="RemoteTimeoutError")
    assert classify({"optimized": lost_lane(), "cold": real}) == "restore_lost"


def test_a_lost_cold_sandbox_is_cold_start_lost_even_if_the_optimized_start_timed_out() -> None:
    real = lane(120, ok=False, error_type="RemoteTimeoutError")
    assert classify({"optimized": real, "cold": lost_lane()}) == "cold_start_lost"


def test_the_total_budget_running_out_in_one_start_is_a_timeout_of_that_start() -> None:
    spent = lane(0, ok=False, error_type="OutOfTime")
    assert classify({"optimized": lane(1), "cold": spent}) == "cold_start_timeout"
    assert classify({"optimized": spent, "cold": lane(1)}) == "restore_timeout"
    assert classify({"optimized": spent, "cold": spent}) == "timeout"


def test_a_busy_worker_names_the_start_it_turned_away() -> None:
    busy = lane(0, ok=False, error_type="ResourceExhaustedError")
    assert classify({"optimized": busy, "cold": lane(1)}) == "restore_busy"
    assert classify({"optimized": lane(1), "cold": busy}) == "cold_start_busy"
    assert classify({"optimized": busy, "cold": busy}) == "busy"


def test_problems_are_looked_for_in_the_order_busy_lost_timeout_failed() -> None:
    busy = lane(0, ok=False, error_type="ResourceExhaustedError")
    timed_out = lane(9, ok=False, error_type="RemoteTimeoutError")
    code_error = lane(1, ok=False, error_type="RemoteExecutionError")
    assert classify({"optimized": lost_lane(), "cold": busy}) == "cold_start_busy"
    assert classify({"optimized": timed_out, "cold": lost_lane()}) == "cold_start_lost"
    assert classify({"optimized": code_error, "cold": timed_out}) == "cold_start_timeout"


def test_an_item_that_loses_its_sandbox_is_not_repeated_as_a_timeout_would_be(
    tmp_path: Path,
) -> None:
    response = {"lanes": {"optimized": lost_lane(), "cold": lane(30)}}
    report = run([item("heavy = 1")], Server(response), tmp_path, repetitions=3)
    assert report.outcomes == {"restore_lost": 3}  # only timeouts stop the repetitions


def test_a_failed_step_and_a_deadline_are_named_by_the_start_that_had_them() -> None:
    assert classify({"optimized": lane(1), "cold": lane(1)}) == "ok"
    assert (
        classify(
            {"optimized": lane(1), "cold": lane(1, ok=False, error_type="RemoteExecutionError")}
        )
        == "cold_start_failed"
    )
    assert (
        classify(
            {
                "optimized": lane(1, ok=False, error_type="RemoteExecutionError"),
                "cold": lane(1, ok=False, error_type="RemoteExecutionError"),
            }
        )
        == "failed"
    )
    assert (
        classify({"optimized": lane(1, ok=False, error_type="RemoteTimeoutError"), "cold": lane(1)})
        == "restore_timeout"
    )
    assert (
        classify({"optimized": lane(1), "cold": lane(1, ok=False, error_type="OutOfTime")})
        == "cold_start_timeout"
    )


def session(pattern: str) -> dict[str, Any]:
    """A lane with one step per letter: ``Y`` succeeded, ``N`` raised in the code."""
    steps = [
        {"index": i, "ok": c == "Y", "duration_s": 1.0, "error": "",
         "error_type": "" if c == "Y" else "RemoteExecutionError",
         "output": "", "truncated": False}
        for i, c in enumerate(pattern)
    ]  # fmt: skip
    return {"session_id": None, "ok": "N" not in pattern, "total_s": 3.0, "steps": steps}


def test_a_fix_item_is_ok_when_its_last_step_succeeds() -> None:
    # Pinned: an agent_fix item runs code that raises and then the steps that repair it,
    # so counting every raise made fix runs that ended repaired look broken.
    both = {"optimized": session("NYY"), "cold": session("NYY")}
    assert classify(both, source="agent_fix") == "ok"
    assert classify(both, source="agent_short") == "failed"
    assert classify(both) == "failed"


def test_a_fix_item_may_break_in_more_than_one_step_before_the_repair() -> None:
    lanes = {"optimized": session("NNYN" + "Y"), "cold": session("NYNY")}
    assert classify(lanes, source="agent_fix") == "ok"


def test_a_fix_item_whose_last_step_raises_is_failed() -> None:
    lanes = {"optimized": session("NYN"), "cold": session("NYN")}
    assert classify(lanes, source="agent_fix") == "failed"


def test_a_fix_item_that_ends_repaired_in_one_start_only_names_the_other_start() -> None:
    lanes = {"optimized": session("NYY"), "cold": session("NYN")}
    assert classify(lanes, source="agent_fix") == "cold_start_failed"
    lanes = {"optimized": session("NYN"), "cold": session("NYY")}
    assert classify(lanes, source="agent_fix") == "restore_failed"


def test_a_fix_item_that_ran_out_of_time_is_still_a_timeout() -> None:
    timed_out = lane(1, ok=False, error_type="RemoteTimeoutError")
    lanes = {"optimized": timed_out, "cold": session("NYY")}
    assert classify(lanes, source="agent_fix") == "restore_timeout"


def test_a_results_file_is_reported_with_the_fix_rule_for_its_source(tmp_path: Path) -> None:
    row = {"id": "a", "source": "agent_fix", "outcome": "failed", "steps": 3,
           "optimized": session("NYY"), "cold": session("NYY")}  # fmt: skip
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps(row) + "\n")
    assert load_results(path)[0]["outcome"] == "ok"


def test_a_server_error_is_recorded_for_that_run_and_the_next_item_still_runs(
    tmp_path: Path,
) -> None:
    server = Server(EvalError("could not reach"), ok_response())
    report = run([item("a = 1"), item("b = 2")], server, tmp_path, repetitions=1)

    first, second = rows(tmp_path)
    assert (first["outcome"], second["outcome"]) == ("error", "ok")
    assert "could not reach" in first["error"]
    assert report.outcomes == {"error": 1, "ok": 1}


def test_long_errors_and_output_are_cut_in_the_file(tmp_path: Path) -> None:
    response = ok_response()
    response["lanes"]["cold"]["steps"][0].update({"error": "e" * 9000, "output": "o" * 9000})
    run([item()], Server(response), tmp_path, repetitions=1)
    step = rows(tmp_path)[0]["cold"]["steps"][0]
    assert len(step["error"]) == 4000
    assert len(step["output"]) == 500


# -- resuming and timeouts --------------------------------------------------------------
def busy_response() -> dict[str, Any]:
    full = lane(0.0, ok=False, error_type="ResourceExhaustedError")
    return {"session_id": None, "lanes": {"optimized": full, "cold": dict(full)}}


def test_a_request_the_worker_had_no_room_for_is_busy_not_failed() -> None:
    assert classify(busy_response()["lanes"]) == "busy"


def test_a_busy_item_is_tried_again_after_a_wait_that_doubles(tmp_path: Path) -> None:
    waits: list[float] = []
    server = Server(busy_response(), busy_response(), ok_response())

    report = run(
        [item()],
        server,
        tmp_path,
        repetitions=1,
        busy_retries=5,
        busy_wait_s=7.0,
        sleep=waits.append,
    )

    assert waits == [7.0, 14.0]
    assert len(server.requests) == 3
    assert [r["outcome"] for r in rows(tmp_path)] == ["ok"]
    assert report.outcomes == {"ok": 1}


def test_an_item_still_busy_after_its_retries_is_recorded_as_busy(tmp_path: Path) -> None:
    waits: list[float] = []
    server = Server(busy_response())

    run(
        [item()],
        server,
        tmp_path,
        repetitions=1,
        busy_retries=2,
        busy_wait_s=1.0,
        sleep=waits.append,
    )

    assert len(server.requests) == 3
    assert waits == [1.0, 2.0]
    assert [r["outcome"] for r in rows(tmp_path)] == ["busy"]


def test_retrying_busy_items_can_be_switched_off(tmp_path: Path) -> None:
    waits: list[float] = []
    run(
        [item()],
        Server(busy_response()),
        tmp_path,
        repetitions=1,
        busy_retries=0,
        sleep=waits.append,
    )
    assert waits == []


def test_a_busy_row_is_tried_again_by_a_later_run_but_a_finished_one_is_not(tmp_path: Path) -> None:
    # Pinned: a worker with every slot held rejected hundreds of items in a row, and
    # resuming then skipped them all because each had a row in the file.
    first = item("a = 1")
    second = item("b = 2")
    run(
        [first, second],
        Server(ok_response(), busy_response()),
        tmp_path,
        repetitions=1,
        busy_retries=0,
    )

    server = Server(ok_response())
    run([first, second], server, tmp_path, repetitions=1, busy_retries=0, resume=True)

    assert [steps for steps, _ in server.requests] == [["b = 2"]]


def test_items_run_at_the_same_time_when_asked_to_and_every_result_is_written(
    tmp_path: Path,
) -> None:
    import threading

    # Three requests must be in flight together for the barrier to open; one at a
    # time would time it out.
    barrier = threading.Barrier(3, timeout=5)

    class Overlapping:
        def run(self, steps: list[str], options: RunOptions) -> dict[str, Any]:
            barrier.wait()
            return ok_response()

    items = [item(f"v{i} = 1") for i in range(3)]
    report = run(items, Overlapping(), tmp_path, repetitions=1, parallel=3)

    assert report.runs == 3
    assert sorted(r["steps"] for r in rows(tmp_path)) == [1, 1, 1]
    assert len({r["id"] for r in rows(tmp_path)}) == 3


def test_a_second_run_continues_where_the_first_stopped_when_asked_to(tmp_path: Path) -> None:
    items = [item("a = 1"), item("b = 2")]
    run(items[:1], Server(ok_response()), tmp_path, repetitions=2)
    server = Server(ok_response())
    report = run(items, server, tmp_path, repetitions=2, resume=True)

    assert report.skipped_done == 2
    assert report.runs == 2
    assert len(server.requests) == 2


def test_a_new_run_starts_the_results_file_over(tmp_path: Path) -> None:
    run([item("a = 1")], Server(ok_response()), tmp_path, repetitions=3)
    report = run([item("b = 2")], Server(ok_response()), tmp_path, repetitions=1)
    assert report.runs == 1
    assert [r["steps"] for r in rows(tmp_path)] == [1]
    assert len(rows(tmp_path)) == 1


def timeout_response() -> dict[str, Any]:
    return {
        "lanes": {
            "optimized": lane(300, ok=False, error_type="RemoteTimeoutError"),
            "cold": lane(300, ok=False, error_type="RemoteTimeoutError"),
        }
    }


def test_an_item_that_times_out_is_not_repeated(tmp_path: Path) -> None:
    server = Server(timeout_response())
    report = run([item("slow = 1")], server, tmp_path, repetitions=3)

    assert len(server.requests) == 1
    assert report.skipped_timeouts == 2
    assert report.outcomes == {"timeout": 1}


def test_repeating_timeouts_can_be_asked_for(tmp_path: Path) -> None:
    server = Server(timeout_response())
    run([item("slow = 1")], server, tmp_path, repetitions=3, skip_timeouts_after=0)
    assert len(server.requests) == 3


def test_progress_is_read_back_from_the_file(tmp_path: Path) -> None:
    ident = item_id(item("slow = 1"))
    run([item("slow = 1")], Server(timeout_response()), tmp_path, repetitions=1)
    done, timeouts = load_progress(tmp_path / "r.jsonl")
    assert done == {(ident, 1)}
    assert timeouts[ident] == 1


# -- selecting -------------------------------------------------------------------
def test_items_can_be_filtered_by_source_domain_id_and_limit() -> None:
    items = [
        item("a = 1", Source.KAGGLE, "Data Science"),
        item("b = 2", Source.AGENT_SHORT, "Computer Vision"),
        item("c = 3", Source.KAGGLE, "Computer Vision"),
    ]
    assert len(select(items, Selection())) == 3
    assert len(select(items, Selection(sources=("kaggle",)))) == 2
    assert [i.code for i in select(items, Selection(domains=("Computer Vision",)))] == [
        ("b = 2",),
        ("c = 3",),
    ]
    assert select(items, Selection(ids=(item_id(items[2])[:6],))) == [items[2]]
    assert len(select(items, Selection(limit=1))) == 1


def test_an_items_id_is_stable_and_depends_only_on_its_code() -> None:
    assert item_id(item("a = 1")) == item_id(item("a = 1", Source.KAGGLE, "Computer Vision"))
    assert item_id(item("a = 1")) != item_id(item("a = 2"))


# -- reporting -------------------------------------------------------------------
def result_rows() -> list[dict[str, Any]]:
    def row(  # noqa: PLR0917
        ident, source, domain, outcome, opt, cold, rep=1, steps=1
    ):
        base = {
            "id": ident,
            "source": source,
            "domain": domain,
            "steps": steps,
            "repetition": rep,
            "outcome": outcome,
        }
        if outcome == "ok":
            base |= {"optimized": lane(opt, steps=steps), "cold": lane(cold, steps=steps)}
        return base

    return [
        row("a", "kaggle", "Data Science", "ok", 1.0, 3.0, rep=1),
        row("a", "kaggle", "Data Science", "ok", 2.0, 5.0, rep=2),
        row("b", "agent_short", "Computer Vision", "ok", 4.0, 4.0),
        row("c", "kaggle", "Data Science", "failed", 0, 0),
        row("d", "kaggle", "Data Science", "timeout", 0, 0),
    ]


def test_each_item_is_reduced_to_the_median_of_its_repetitions() -> None:
    summaries = {s.id: s for s in summarise_items(result_rows())}
    assert set(summaries) == {"a", "b"}  # failed and timed-out items are left out
    assert (summaries["a"].optimized_s, summaries["a"].cold_s) == (1.5, 4.0)
    assert summaries["a"].repetitions == 2
    assert summaries["a"].saved_s == 2.5
    assert summaries["a"].speedup == pytest.approx(4.0 / 1.5)


def test_items_are_grouped_into_duration_buckets() -> None:
    labels = [bucket(s) for s in (1, 5, 29, 30, 119, 120, 900)]
    assert labels == [
        "under 5 s",
        "5 to 30 s",
        "5 to 30 s",
        "30 to 120 s",
        "30 to 120 s",
        "over 120 s",
        "over 120 s",
    ]


def test_the_report_lists_outcomes_groups_and_problems() -> None:
    text = format_report(result_rows())
    assert "5 runs over 4 items" in text
    assert "failed 1" in text
    assert "ok 3" in text
    assert "timeout: 1 runs" in text
    assert "by source" in text
    assert "by domain" in text
    assert "by cold duration" in text
    assert "kaggle" in text
    assert "optimized faster in" in text


def test_the_report_counts_each_problem_by_the_starts_that_had_it() -> None:
    def row(
        ident: str, outcome: str, optimized: dict[str, Any], cold: dict[str, Any]
    ) -> dict[str, Any]:
        return {"id": ident, "source": "kaggle", "domain": DOMAIN, "steps": 1, "repetition": 1,
                "outcome": outcome, "optimized": optimized, "cold": cold}  # fmt: skip

    rows_ = [
        *result_rows(),
        row("z", "restore_lost", lost_lane(), lane(30)),
        {**row("z", "restore_lost", lost_lane(), lane(31)), "repetition": 2},
        row("y", "cold_start_lost", lane(30), lost_lane()),
        row("w", "lost", lost_lane(), lost_lane()),
    ]
    text = format_report(rows_)

    assert (
        "lost: 4 runs (1 in both starts, 2 in the restore only, 1 in the cold start only)" in text
    )
    assert (
        "failed: 1 runs (1 in both starts, 0 in the restore only, 0 in the cold start only)" in text
    )
    assert "busy: 0 runs" in text
    assert {s.id for s in summarise_items(rows_)} == {"a", "b"}


def test_the_report_of_an_empty_file_does_not_fail() -> None:
    assert "0 runs" in format_report([])


def test_the_first_step_of_multi_step_items_is_reported_separately() -> None:
    rows_ = [
        {"id": "n", "source": "kaggle_notebook", "domain": DOMAIN, "steps": 3, "repetition": 1,
         "outcome": "ok", "optimized": lane(3.0, steps=3), "cold": lane(9.0, steps=3)},
    ]  # fmt: skip
    assert "first step of the multi-step items" in format_report(rows_)


def test_a_results_file_written_under_an_older_rule_is_reported_under_the_current_one(
    tmp_path: Path,
) -> None:
    # The trial file recorded this run as a timeout, before restore_lost existed.
    old = {"id": "q", "source": "kaggle", "domain": DOMAIN, "steps": 1, "repetition": 1,
           "outcome": "timeout", "optimized": lost_lane(), "cold": lane(30)}  # fmt: skip
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps(old) + "\n" + json.dumps({"id": "e", "outcome": "error"}) + "\n")

    rows_ = load_results(path)

    assert [r["outcome"] for r in rows_] == ["restore_lost", "error"]
    assert "lost: 1 runs (0 in both starts, 1 in the restore only" in format_report(rows_)


def test_load_results_skips_lines_that_are_not_json(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text('{"id": "a"}\nnot json\n\n[1]\n{"id": "b"}\n')
    assert [r["id"] for r in load_results(path)] == ["a", "b"]
