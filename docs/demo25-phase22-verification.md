# Demo 2.5 Phase 2.2 baseline-aware verification

Registered browser tasks use two verification layers. Changed test files under `tests/`
are run as task-specific tests. The configured broad pytest target is also run in the
task workspace and in a temporary pristine export of the registered repository.

The baseline is keyed by repository identity, immutable source commit, Python runtime,
and verification configuration. New tasks record the resolved source commit when their
workspace is provisioned. Older tasks resolve their stored repository and base branch
lazily, so they do not need recreation. Successful baselines are cached in-process;
concurrent creation for one key is serialized. Baseline workspaces are temporary and
never reuse a task workspace or mutate the registered source.

Pytest failures are normalized to node IDs. For baseline failures `B` and current
failures `C`, pre-existing failures are `B ∩ C`, fixed failures are `B - C`, and new
regressions are `C - B`. Task-specific failures and new regressions block verification;
pre-existing failures are warnings and fixed failures are informational. Therefore a
nonzero broad-suite exit can still produce an overall pass when it contains only known
baseline failures. Timeout, collection/usage, and missing structured results fail closed
as infrastructure errors.

Predefined legacy DEMO tasks retain their explicit verification behavior. Phase 2.2
does not alter lifecycle exhaustion, model routing, or implement autonomous phases.
