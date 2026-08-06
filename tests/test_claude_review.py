"""Tests for the Claude review wrapper."""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "claude-for-codex"
    / "scripts"
    / "claude-review"
)


def load_script():
    """Load the extensionless Python entrypoint as a module."""
    loader = importlib.machinery.SourceFileLoader("claude_review", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


claude_review = load_script()


class ClaudeReviewTests(unittest.TestCase):
    def test_command_is_read_only(self) -> None:
        args = argparse.Namespace(
            claude_binary="/usr/local/bin/claude",
            model="opus",
            effort="high",
            allow_repo_read=False,
            max_budget_usd=2.0,
        )

        command = claude_review.build_claude_command(args)

        self.assertIn("--permission-mode", command)
        self.assertIn("--verbose", command)
        self.assertEqual(
            command[command.index("--output-format") + 1],
            "stream-json",
        )
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertNotIn("Edit", command)
        self.assertNotIn("Bash", command)
        self.assertIn("--safe-mode", command)
        self.assertIn("--no-chrome", command)
        self.assertIn("--no-session-persistence", command)
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "2.0")

    def test_repo_read_is_explicit_opt_in(self) -> None:
        args = argparse.Namespace(
            claude_binary="/usr/local/bin/claude",
            model=None,
            effort=None,
            allow_repo_read=True,
            max_budget_usd=1.0,
        )

        command = claude_review.build_claude_command(args)

        self.assertEqual(
            command[command.index("--tools") + 1],
            claude_review.READ_ONLY_TOOLS,
        )

    def test_diff_commands_exclude_common_secret_paths(self) -> None:
        responses = [
            mock.Mock(stdout=" M app.py\n M .env\n"),
            mock.Mock(stdout="diff"),
            mock.Mock(stdout=""),
        ]
        with mock.patch.object(claude_review, "run", side_effect=responses) as runner:
            _, context = claude_review.collect_review_context(
                Path("/repo"),
                base=None,
                max_diff_bytes=10_000,
            )

        diff_command = runner.call_args_list[1].args[0]
        self.assertIn(":(exclude,glob).env", diff_command)
        self.assertIn(":(exclude,glob)**/*.pem", diff_command)
        self.assertIn("excluded from both this list and the supplied diff", context)

    def test_extracts_structured_output(self) -> None:
        expected = {
            "verdict": "approve",
            "summary": "Looks good.",
            "findings": [],
            "coverage_gaps": [],
            "next_steps": [],
        }

        actual = claude_review.extract_review(
            json.dumps({"type": "result", "structured_output": expected})
        )

        self.assertEqual(actual, expected)

    def test_extracts_structured_output_from_stream(self) -> None:
        expected = {
            "verdict": "approve",
            "summary": "Looks good.",
            "findings": [],
            "coverage_gaps": [],
            "next_steps": [],
        }
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "model": "claude-sonnet-4-6",
                    }
                ),
                json.dumps({"type": "assistant", "message": {"content": []}}),
                json.dumps({"type": "result", "structured_output": expected}),
            ]
        )

        self.assertEqual(claude_review.extract_stream_review(stream), expected)

    def test_progress_event_does_not_expose_tool_input(self) -> None:
        event = claude_review.progress_event(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/repo/private-name.py"},
                        }
                    ]
                },
            }
        )

        self.assertEqual(
            event,
            (
                "tool:Read",
                "Claude is inspecting repository context with Read.",
            ),
        )
        assert event is not None
        self.assertNotIn("private-name.py", event[1])

    def test_stream_runner_reports_truthful_heartbeat(self) -> None:
        review = {
            "verdict": "approve",
            "summary": "Looks good.",
            "findings": [],
            "coverage_gaps": [],
            "next_steps": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            fake_claude = Path(directory) / "fake-claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import sys\n"
                "import time\n"
                "sys.stdin.read()\n"
                "time.sleep(0.08)\n"
                f"print(json.dumps({{'type': 'result', "
                f"'structured_output': {review!r}}}), flush=True)\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            progress = io.StringIO()

            with mock.patch.object(claude_review, "PROGRESS_INTERVAL_SECONDS", 0.02):
                result = claude_review.run_claude_stream(
                    [str(fake_claude)],
                    cwd=Path(directory),
                    input_text="review this",
                    timeout=1,
                    context_size=11,
                    requested_model=None,
                    effort=None,
                    progress_stream=progress,
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(claude_review.extract_stream_review(result.stdout), review)
        self.assertIn("Claude is still reviewing", progress.getvalue())
        self.assertIn("No completion estimate is available", progress.getvalue())

    def test_stream_runner_heartbeats_while_events_are_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_claude = Path(directory) / "fake-claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import sys\n"
                "sys.stdin.read()\n"
                "for index in range(20000):\n"
                "    print(json.dumps({'type': 'system', 'subtype': "
                "'activity', 'index': index}), flush=True)\n"
                "\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            progress = io.StringIO()

            with mock.patch.object(claude_review, "PROGRESS_INTERVAL_SECONDS", 0.001):
                result = claude_review.run_claude_stream(
                    [str(fake_claude)],
                    cwd=Path(directory),
                    input_text="review this",
                    timeout=1,
                    context_size=11,
                    requested_model=None,
                    effort=None,
                    progress_stream=progress,
                )

        self.assertEqual(result.returncode, 0)
        self.assertGreaterEqual(
            progress.getvalue().count("Claude is still reviewing"),
            2,
        )

    def test_stream_runner_bounds_stalled_stdin_by_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_claude = Path(directory) / "fake-claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import time\n"
                "time.sleep(2)\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)

            started_at = claude_review.time.monotonic()
            with self.assertRaisesRegex(claude_review.ReviewError, "timed out"):
                claude_review.run_claude_stream(
                    [str(fake_claude)],
                    cwd=Path(directory),
                    input_text="x" * 400_000,
                    timeout=0.1,
                    context_size=400_000,
                    requested_model=None,
                    effort=None,
                    progress_stream=io.StringIO(),
                )

        self.assertLess(claude_review.time.monotonic() - started_at, 1)

    def test_stream_runner_handles_early_exit_without_broken_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_claude = Path(directory) / "fake-claude"
            fake_claude.write_text(
                "#!/bin/sh\n"
                "echo 'unsupported option' >&2\n"
                "exit 2\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)

            result = claude_review.run_claude_stream(
                [str(fake_claude)],
                cwd=Path(directory),
                input_text="x" * 400_000,
                timeout=1,
                context_size=400_000,
                requested_model=None,
                effort=None,
                progress_stream=io.StringIO(),
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported option", result.stderr)

    def test_failure_detail_uses_only_final_stdout_error(self) -> None:
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": "private partial reasoning"},
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "error_max_budget_usd",
                        "is_error": True,
                        "result": "Maximum budget reached.",
                    }
                ),
            ]
        )

        detail = claude_review.failure_detail("", stdout)

        self.assertIn("error_max_budget_usd", detail)
        self.assertIn("Maximum budget reached", detail)
        self.assertNotIn("private partial reasoning", detail)

    def test_failure_detail_handles_missing_error_event(self) -> None:
        self.assertEqual(
            claude_review.failure_detail("", ""),
            "Claude review failed without an error message.",
        )

    def test_extract_stream_review_explains_missing_result(self) -> None:
        stream = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps({"type": "assistant", "message": {"content": []}}),
            ]
        )

        with self.assertRaisesRegex(
            claude_review.ReviewError,
            "ended before returning a final result event",
        ):
            claude_review.extract_stream_review(stream)

    def test_rejects_incomplete_structured_output(self) -> None:
        with self.assertRaisesRegex(claude_review.ReviewError, "missing required"):
            claude_review.extract_review(
                json.dumps({"structured_output": {"verdict": "approve"}})
            )

    def test_run_converts_timeout_to_review_error(self) -> None:
        with mock.patch.object(
            claude_review.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["claude"], 1),
        ):
            with self.assertRaisesRegex(claude_review.ReviewError, "timed out"):
                claude_review.run(["claude"], timeout=1)

    def test_rejects_oversized_context(self) -> None:
        responses = [
            mock.Mock(stdout=" M file.py\n"),
            mock.Mock(stdout="x" * 100),
            mock.Mock(stdout=""),
        ]
        with mock.patch.object(claude_review, "run", side_effect=responses):
            with self.assertRaisesRegex(claude_review.ReviewError, "safety limit"):
                claude_review.collect_review_context(
                    Path("/repo"),
                    base=None,
                    max_diff_bytes=32,
                )

    def test_working_tree_context_includes_staged_and_unstaged_changes(self) -> None:
        responses = [
            mock.Mock(stdout=" M a.py\nM  b.py\n"),
            mock.Mock(stdout="unstaged"),
            mock.Mock(stdout="staged"),
        ]
        with mock.patch.object(claude_review, "run", side_effect=responses):
            scope, context = claude_review.collect_review_context(
                Path("/repo"),
                base=None,
                max_diff_bytes=10_000,
            )

        self.assertIn("working-tree", scope)
        self.assertIn("unstaged", context)
        self.assertIn("staged", context)

    def test_check_reports_missing_binary(self) -> None:
        with mock.patch.object(claude_review.shutil, "which", return_value=None):
            self.assertEqual(claude_review.check_claude("claude"), 1)

    def test_check_explains_macos_sandbox_auth_false_negative(self) -> None:
        version = mock.Mock(stdout="2.1.221\n", stderr="", returncode=0)
        auth = mock.Mock(
            stdout=json.dumps({"loggedIn": False}),
            stderr="",
            returncode=1,
        )

        with (
            mock.patch.object(claude_review.shutil, "which", return_value="/bin/claude"),
            mock.patch.object(claude_review, "run", side_effect=[version, auth]),
            mock.patch.object(claude_review.sys, "platform", "darwin"),
            mock.patch("builtins.print") as printer,
        ):
            self.assertEqual(claude_review.check_claude("claude"), 1)

        output = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("scoped sandbox escalation", output)
        self.assertIn("Login Keychain", output)

    def test_main_uses_fake_claude_in_a_git_repo(self) -> None:
        review = {
            "verdict": "approve",
            "summary": "No actionable issues.",
            "findings": [],
            "coverage_gaps": [],
            "next_steps": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
            claude = repo / "fake-claude"
            claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            claude.chmod(0o755)

            run_result = mock.Mock(returncode=0, stdout=json.dumps(review), stderr="")
            with (
                mock.patch.object(claude_review.shutil, "which", return_value=str(claude)),
                mock.patch.object(claude_review, "find_repo_root", return_value=repo),
                mock.patch.object(
                    claude_review,
                    "collect_review_context",
                    return_value=("working tree", "diff"),
                ),
                mock.patch.object(
                    claude_review,
                    "run_claude_stream",
                    return_value=run_result,
                ) as runner,
                mock.patch("builtins.print"),
            ):
                exit_code = claude_review.main(["--claude-binary", str(claude), "--json"])

            self.assertEqual(exit_code, 0)
            command = runner.call_args.args[0]
            self.assertEqual(command[0], str(claude))
            self.assertIn("--tools", command)

    def test_entrypoint_reviews_real_git_diff_without_env_contents(self) -> None:
        review = {
            "verdict": "approve",
            "summary": "No actionable issues.",
            "findings": [],
            "coverage_gaps": [],
            "next_steps": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo,
                check=True,
            )
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            (repo / ".env").write_text("TOKEN=before\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "initial"],
                cwd=repo,
                check=True,
            )

            (repo / "app.py").write_text("value = 2\n", encoding="utf-8")
            (repo / ".env").write_text("TOKEN=do-not-send\n", encoding="utf-8")
            captured_prompt = repo / "prompt.txt"
            fake_claude = repo / "fake-claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "from pathlib import Path\n"
                "import sys\n"
                f"Path({str(captured_prompt)!r}).write_text("
                "sys.stdin.read(), encoding='utf-8')\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', "
                "'model': 'claude-test'}), flush=True)\n"
                "print(json.dumps({'type': 'assistant', 'message': {'content': ["
                "{'type': 'tool_use', 'name': 'Read', "
                "'input': {'file_path': '/private/path.py'}}]}}), flush=True)\n"
                f"print(json.dumps({{'type': 'result', "
                f"'structured_output': {review!r}}}), flush=True)\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)

            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--claude-binary",
                    str(fake_claude),
                    "--json",
                ],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), review)
            self.assertIn("Started review", result.stderr)
            self.assertIn("Claude initialized using claude-test", result.stderr)
            self.assertIn("inspecting repository context with Read", result.stderr)
            self.assertIn("process finished after", result.stderr)
            self.assertNotIn("/private/path.py", result.stderr)
            prompt = captured_prompt.read_text(encoding="utf-8")
            self.assertIn("value = 2", prompt)
            self.assertNotIn("do-not-send", prompt)
            self.assertNotIn("M .env", prompt)


if __name__ == "__main__":
    unittest.main()
