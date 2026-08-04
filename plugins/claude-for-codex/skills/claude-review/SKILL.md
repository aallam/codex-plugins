---
name: claude-review
description: Ask the local Claude Code CLI for an independent, read-only review or second opinion on current code changes. Use when the user asks Codex to have Claude review the working tree, compare the branch to a base ref, challenge an implementation, or focus a review on a risk area.
---

# Claude Review

Use the bundled reviewer to ask Claude for a second opinion while Codex remains
the orchestrator.

## Safety contract

- Tell the user before invoking Claude that repository code and diffs will be
  sent to their configured Claude provider.
- Keep the run read-only. By default the bundled script disables all Claude
  tools and supplies only a filtered diff; never replace that command with an
  unrestricted `claude` invocation.
- Do not use this skill for secrets, credentials, private keys, environment
  files, or repositories the user is not authorized to send to Claude.
- Treat Claude's result as reviewer input, not as verified truth. Check every
  reported finding against the checkout before presenting it as confirmed.

## Locate the bundled script

Resolve this `SKILL.md` from the skill source path shown by Codex. From the
skill directory, the script is at `../../scripts/claude-review`; it is not
relative to the user's repository:

```text
<plugin-root>/scripts/claude-review
```

Run it with the user's repository as the current working directory.

## Workflow

1. Run setup diagnostics:

   ```bash
   <plugin-root>/scripts/claude-review --check
   ```

   On macOS, run this exact command with a scoped sandbox escalation. Claude
   Code commonly stores authentication in Login Keychain, which is unavailable
   to a sandboxed subprocess and can produce a false "not logged in" result.
   If a check was already run inside the sandbox and failed authentication,
   treat it as inconclusive and retry it once outside the sandbox. If the
   escalated check still fails, report the exact diagnostic and stop. Do not
   start an interactive login without the user's request.

2. Choose exactly one review scope:

   - Current staged, unstaged, and untracked changes:

     ```bash
     <plugin-root>/scripts/claude-review
     ```

   - Commits on the current branch compared with a base ref:

     ```bash
     <plugin-root>/scripts/claude-review --base main
     ```

3. Add `--adversarial` when the user wants assumptions, architecture, or
   failure modes challenged.

4. Add `--focus "<text>"` for a specific concern such as authentication, data
   loss, concurrency, backwards compatibility, or rollback.

5. Optional user-selected Claude controls:

   ```bash
   <plugin-root>/scripts/claude-review --model opus --effort high
   ```

   Do not choose a paid model or increase effort unless the user asks or their
   existing Claude defaults already do so.

6. Add `--allow-repo-read` only when the user explicitly authorizes Claude to
   inspect repository files beyond the supplied diff. This enables `Read`,
   `Glob`, and `Grep`, but remains read-only.

7. On macOS, run the selected review command with the same scoped sandbox
   escalation used for `--check`; the review process needs the same Keychain
   credential. Escalate only the bundled wrapper command, not the whole Codex
   session. The wrapper still restricts Claude to its documented read-only
   tools and filtered context.

8. Verify Claude's findings locally:

   - Open the cited files and lines.
   - Confirm the behavior is introduced by the reviewed changes.
   - Discard style-only, speculative, or pre-existing issues.
   - Lead with confirmed findings ordered by severity.

## Examples

```bash
<plugin-root>/scripts/claude-review
<plugin-root>/scripts/claude-review --base main
<plugin-root>/scripts/claude-review --adversarial --focus "auth and tenant isolation"
<plugin-root>/scripts/claude-review --base origin/main --focus "rollback safety"
```

## Output

The script returns a concise Markdown review by default. Use `--json` only when
machine-readable findings are materially useful. If Claude returns no findings,
say so and mention any coverage gaps Claude identified.
