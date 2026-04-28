---
description: Run the 8-agent code analysis pipeline. Usage: /analyze [git-ref]. With a ref → diff mode. Without → working-tree mode.
allowed-tools: Task, mcp__codeanalysis__query_findings, mcp__codeanalysis__query_modifications
argument-hint: "[git-ref]"
---

# /analyze pipeline

Argument: `$ARGUMENTS`

You are orchestrating an 8-agent code analysis pipeline. **Strict 3-phase orchestration** — do not improvise the order, do not skip the parallel fan-out.

## Phase 1 — ANALYZER (sequential, barrier)

Invoke the `analyzer` subagent in **one** Task call. Pass it as the prompt:

- If `$ARGUMENTS` is empty → tell it `MODE=worktree, base_ref=None`.
- Otherwise → tell it `MODE=diff, base_ref=$ARGUMENTS`.

Wait for it to complete. From its output, extract `RUN_ID=<n>`. **Do not proceed without a run_id.**

## Phase 2 — Fan-out (parallel, single message)

In a **single message** issue **six** Task calls in parallel — IMPACT, SECURITY, PERF, TESTS, DEPS, QUALITY. Each receives the same prompt: `run_id=<RUN_ID>`. Issuing them in one message is what makes them run concurrently — do not serialize them across multiple messages.

The agents do not communicate with each other; they all write to the shared SQLite database via MCP. Wait for all six to finish.

## Phase 3 — VERDICT (sequential)

Invoke the `verdict` subagent with `run_id=<RUN_ID>`. Display its output verbatim to the user — that is the final report.

## Constraints
- Do not write findings yourself. Do not call `record_finding` directly. The subagents own that.
- If Phase 1 fails or returns no `run_id`, abort and report the error. Do not run Phase 2.
- The order matters: ANALYZER must finish before fan-out, and VERDICT must run after fan-out completes.
