# Demo 2.5 Phase 2.1 model routing

Phase 2.1 adds durable model intent and deterministic resolution; it does not run the
autonomous phase workflow. Tasks store a logical default (`AUTO`, `CLAUDE_SONNET`, or
`CLAUDE_OPUS`). Brainstorm, Plan, Implementation, and Review may store an independent
logical override. Human Review never has an agent model.

Resolution is: concrete phase override, concrete task default, platform AUTO policy,
then the catalog's configured provider target. An absent or `AUTO` phase override
inherits the task default. An `AUTO` task uses Sonnet for Brainstorm, Plan, and
Implementation, and Opus for Review. Concrete provider IDs are configured centrally
with `AI_PLATFORM_CLAUDE_SONNET_MODEL` and `AI_PLATFORM_CLAUDE_OPUS_MODEL`; the legacy
`AI_PLATFORM_DEFAULT_MODEL` and `AI_PLATFORM_STRONG_MODEL` names remain compatible.
Unavailable concrete selections fail before execution and never silently fall back.

Preference changes are atomic and emit durable actor-attributed events. Each current
compatibility coding turn resolves as Implementation and snapshots its target before
execution, so a concurrent preference edit applies only to the next turn. Execution
events record the requested logical selection, effective logical model, provider,
concrete model ID, resolution source, and compatibility phase. Provider/session context
is not represented by the model preference, leaving future independent Review contexts
possible and additional providers isolated behind the catalog and resolver boundary.
