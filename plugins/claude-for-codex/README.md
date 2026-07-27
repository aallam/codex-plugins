# Claude for Codex

Ask Claude for an independent code review without leaving Codex.

This is the inverse of
[`openai/codex-plugin-cc`](https://github.com/openai/codex-plugin-cc): Codex
stays in control and invokes your local Claude Code CLI as a read-only reviewer.

## What you get

- Review staged, unstaged, and untracked working-tree changes.
- Compare committed branch changes with a base ref such as `main`.
- Run an adversarial review that challenges design choices and failure modes.
- Focus Claude on concerns such as authentication, concurrency, rollback, or
  backwards compatibility.
- Receive structured findings that Codex verifies against the checkout.

Claude gets no tools by default and reviews only a filtered diff. The optional
`--allow-repo-read` flag grants `Read`, `Glob`, and `Grep` when you explicitly
want deeper context. The wrapper never grants `Bash`, edit tools, web tools, or
session persistence. It also enables Claude safe mode so repository/user hooks,
plugins, MCP servers, and CLAUDE.md files do not participate in the review.

## Requirements

- Codex CLI with plugin support.
- Git.
- Claude Code CLI installed and authenticated.

Check Claude directly:

```bash
claude --version
claude auth status
```

If needed:

```bash
claude auth login
```

## Install

First add the marketplace:

```bash
codex plugin marketplace add aallam/codex-plugins
```

Then install the plugin:

```bash
codex plugin add claude-for-codex@aallam-codex-plugins
```

Start a new Codex session so the bundled skill is loaded.

## Use

Ask naturally:

```text
Ask Claude to review my current changes.
Have Claude review this branch against main.
Ask Claude for an adversarial review focused on auth and tenant isolation.
Get a Claude second opinion on rollback safety.
```

Or invoke the bundled skill explicitly with `$claude-review`.

The underlying wrapper can also be run directly from the repository root:

```bash
plugins/claude-for-codex/scripts/claude-review --check
plugins/claude-for-codex/scripts/claude-review
plugins/claude-for-codex/scripts/claude-review --base main
plugins/claude-for-codex/scripts/claude-review \
  --adversarial \
  --focus "race conditions and retry behavior"
```

Use `--model` or `--effort` only when you intentionally want to override your
Claude defaults. Use `--allow-repo-read` only when you intentionally want
Claude to inspect files beyond the filtered diff. Use `--json` for
machine-readable output.

## Privacy and safety

Running a review sends the selected diff and any files Claude chooses to inspect
to your configured Claude provider. Do not use it for code you are not
authorized to share. The prompt tells Claude not to open secret-bearing files,
but prompt instructions are not a data-loss-prevention boundary.

The wrapper:

- limits the initial review context to 400 KB;
- excludes common secret-bearing paths from both the change list and diff;
- disables all Claude tools unless repository reads are explicitly enabled;
- runs Claude in plan mode and never enables write or shell tools;
- enables safe mode and disables Chrome integration;
- disables Claude session persistence;
- caps a run at 10 minutes and a $2 API budget by default;
- never applies Claude's suggestions automatically;
- asks Codex to verify findings locally before reporting them.

## Development

From the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 /path/to/plugin-creator/scripts/validate_plugin.py \
  plugins/claude-for-codex
```

## Scope

The first release intentionally stays synchronous and review-only. Background
jobs, writable delegation, and automatic review gates add state and permission
risk; they can be layered on after this core flow is proven reliable.
