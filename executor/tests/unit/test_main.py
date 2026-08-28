"""Executor modes."""

from __future__ import annotations

import os
from unittest.mock import patch

from crfs_executor.preimport import PreimportReport
from crfs_executor.__main__ import main


@patch("crfs_executor.__main__.preimport")
@patch("crfs_executor.__main__.announce_ready")
@patch("crfs_executor.__main__.await_checkpoint")
@patch("crfs_executor.__main__.ExecutorServer.serve_forever")
def test_capture_mode(
    mock_serve, mock_await, mock_announce, mock_preimport, tmp_path
):
    mock_preimport.return_value = PreimportReport(loaded=["math", "random"], failed={})

    # Set up the environment dictionary
    env = {
        # tmp_path ensures bind() creates a real socket safely
        "EXECUTOR_DIR": str(tmp_path),
        "EXECUTOR_MODE": "capture",
        "CRFS_PREIMPORT": "math,random",
        "CRFS_CHECKPOINT_SLEEP": "5",
    }

    with patch.dict(os.environ, env, clear=True):
        exit_code = main()

    assert exit_code == 0
    mock_preimport.assert_called_once_with(("math", "random"))
    mock_announce.assert_called_once_with(mock_preimport.return_value)
    mock_await.assert_called_once_with(5)
    mock_serve.assert_called_once()


@patch("crfs_executor.__main__.preimport")
@patch("crfs_executor.__main__.announce_ready")
@patch("crfs_executor.__main__.await_checkpoint")
@patch("crfs_executor.__main__.ExecutorServer.serve_forever")
def test_analyze_mode(
    mock_serve, mock_await, mock_announce, mock_preimport, tmp_path
):
    env = {
        "EXECUTOR_DIR": str(tmp_path),
        "EXECUTOR_MODE": "analyze",
        "CRFS_PREIMPORT": "math,random",
    }

    with patch.dict(os.environ, env, clear=True):
        exit_code = main()

    assert exit_code == 0
    mock_preimport.assert_called_once_with(("math", "random"))

    # Analyze mode exits early, so these should NOT be called
    mock_announce.assert_not_called()
    mock_await.assert_not_called()
    mock_serve.assert_not_called()


@patch("crfs_executor.__main__.preimport")
@patch("crfs_executor.__main__.ExecutorServer.serve_forever")
def test_baseline_mode(mock_serve, mock_preimport, tmp_path):
    env = {
        "EXECUTOR_DIR": str(tmp_path),
        "EXECUTOR_MODE": "baseline",
    }

    with patch.dict(os.environ, env, clear=True):
        exit_code = main()

    assert exit_code == 0
    mock_preimport.assert_not_called()
    mock_serve.assert_not_called()


@patch("crfs_executor.__main__.preimport")
@patch("crfs_executor.__main__.ExecutorServer.serve_forever")
def test_default_run_mode(mock_serve, mock_preimport, tmp_path):
    env = {
        "EXECUTOR_DIR": str(tmp_path),
        "EXECUTOR_MODE": "run",  # Any unrecognized mode falls through to the server
    }

    with patch.dict(os.environ, env, clear=True):
        exit_code = main()

    assert exit_code == 0
    mock_preimport.assert_not_called()

    # Server should bind and block
    mock_serve.assert_called_once()
