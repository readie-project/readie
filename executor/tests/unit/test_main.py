"""Executor modes."""

from __future__ import annotations

import os
from unittest.mock import patch

from crfs_executor.preimport import PreimportReport
from crfs_executor.__main__ import main


@patch("crfs_executor.__main__.preimport")
@patch("crfs_executor.__main__.trigger_checkpoint")
@patch("crfs_executor.__main__.ExecutorServer.serve_forever")
def test_capture_mode(
    mock_serve, mock_trigger, mock_preimport, tmp_path
):
    mock_preimport.return_value = PreimportReport(loaded=["math", "random"], failed={})

    # Set up the environment dictionary
    env = {
        # tmp_path ensures bind() creates a real socket safely
        "EXECUTOR_DIR": str(tmp_path),
        "EXECUTOR_MODE": "capture",
        "CRFS_PREIMPORT": "math,random",
    }

    with patch.dict(os.environ, env, clear=True):
        exit_code = main()

    assert exit_code == 0
    mock_preimport.assert_called_once_with(("math", "random"))
    mock_trigger.assert_called_once_with(mock_preimport.return_value)
    mock_serve.assert_called_once()


@patch("crfs_executor.__main__.preimport")
@patch("crfs_executor.__main__.trigger_checkpoint")
@patch("crfs_executor.__main__.ExecutorServer.serve_forever")
def test_measure_mode(
    mock_serve, mock_trigger, mock_preimport, tmp_path
):
    env = {
        "EXECUTOR_DIR": str(tmp_path),
        "EXECUTOR_MODE": "measure",
        "CRFS_PREIMPORT": "math,random",
    }

    with patch.dict(os.environ, env, clear=True):
        exit_code = main()

    assert exit_code == 0

    # Measure mode exits early, so these should NOT be called
    mock_preimport.assert_not_called()
    mock_trigger.assert_not_called()
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
