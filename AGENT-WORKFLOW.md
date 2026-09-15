# Agent workflow

## Task routing

- **Mechanical:** locate the target, patch it, verify the exact old/new pattern,
  and report. Do not add design work unless risk requires it.
- **Debug:** reproduce once, inspect evidence, form a hypothesis, make the
  smallest fix, run a focused test, then the relevant broader check. State what
  changed before rerunning an expensive failed command.
- **Feature:** create a branch or worktree, plan, use TDD, implement, and
  validate.
- **UI:** use browser or visual verification; use native GUI automation only for
  a concrete inspection goal.
- **Docs/report:** refresh data and produce attributed Markdown; only perform
  diff hygiene beyond documentation review.

Use higher reasoning only for architecture, multi-step debugging, or uncertain
design, and state that escalation at task start.

## Ownership and delegation

The primary agent owns architecture, public tool contracts, cross-module work,
security-sensitive changes, authentication, integration, final review, and
acceptance criteria.

Delegate substantial, bounded, decision-complete work such as focused tests,
mechanical refactors, documentation, fixtures, call-site research, formatting,
simple type fixes, and schema comparison. Do not delegate tiny edits or tightly
coupled work without explicit boundaries. Give delegates scope, files or
subsystem, expected behavior, constraints, tests, and exclusions. Require their
summary, files changed, tests run, and remaining concerns; review their output
and independently verify relevant results before integration.

## Development principles

Prefer boring explicit code, small public APIs, stable schemas, server-side
filtering where available, and early-stop client pagination where it is not.
Avoid speculative functionality, trivial new dependencies, and startup-time
micro-optimizations without measurement. Preserve compatibility over convenience
unless there is a strong reason for a deliberate breaking change.
