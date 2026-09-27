# Demo 2.5.1 canonical verification hotfix

## Root-cause audit

The Implementation provider returned four structured fields: `summary`,
`files_changed`, `tests_run`, and `known_issues`. Before this hotfix the graph
already discarded the provider's file and test lists and replaced them with
workspace-derived paths and a coarse platform-generated `passed`/`failed`
line. It still persisted and rendered `summary` verbatim.

The production provider put an unqualified full-suite success claim in that
narrative even though canonical baseline-aware verification recorded a passing
focused test, three unchanged baseline failures, and zero new regressions. The
UI rendered the narrative before the platform line, creating contradictory
user-facing evidence.

Review received the whole Implementation Summary, the actual diff, and only
the task-level verification word `passed`. It did not receive focused-test
counts, baseline comparison, new-regression classification, the executed
command, or an explicit authority hierarchy. The independent reviewer therefore
treated the provider narrative as evidence and correctly stopped on the
apparent contradiction.

## Boundary after the hotfix

The latest persisted TEST event and current workspace are authoritative. New
Implementation Summary versions contain workspace-derived changed files and a
canonical verification object populated only by the graph. Provider narrative
is retained as explicitly descriptive implementation notes.

Review receives authoritative changed files, persisted verification metadata,
baseline/new-regression facts, source commit, actual diff, requirements, and
Plan separately from non-authoritative narrative. Historical artifacts remain
append-only and are not rewritten; Review applies the same hierarchy when it
encounters a legacy summary.

No database migration, phase change, threshold change, or readiness override is
required.
