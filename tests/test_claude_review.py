"""Tests for the Claude review wrapper."""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
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
                mock.patch.object(claude_review, "run", return_value=run_result) as runner,
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
                f"print(json.dumps({{'structured_output': {review!r}}}))\n",
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
            prompt = captured_prompt.read_text(encoding="utf-8")
            self.assertIn("value = 2", prompt)
            self.assertNotIn("do-not-send", prompt)
            self.assertNotIn("M .env", prompt)


if __name__ == "__main__":
    unittest.main()
