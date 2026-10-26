# n8n-claw Memory Delete knowledge cards

These team-authored cards summarize the exact upstream revisions and the limited replay already
recorded for issue #75. The upstream workflow has no separately verified license file, so the
cards contain commit URLs, source sizes, and SHA-256 values only. They do not copy the workflow,
JavaScript, or author tests.

## Cards

- `memory-delete-owner-check-gap.json` covers the feature-add revision
  `63a7f2ee9c9e42fc6a3c4527fa2a7b7f12859377`. Static inspection found no requester, owner, scope,
  or admin check before the direct `memory_long` delete. The fixed replay observed the foreign
  synthetic row being deleted.
- `memory-delete-owner-check-first-fix.json` covers the separate comparison
  `4d1b1b748cdc76f6c61abb7c40ee8b01b560ef44` →
  `eb6d7bc185eaecdf96885b3e17ef1f8dac93a852`. The commit is the upstream finding and first-fix
  record. Its Memory Delete code checks the selected row, caller ID, and admin list. The replay
  denied a foreign personal row and allowed the tested owner and admin cases. A later revert
  restored the earlier workflow hash and the foreign-row deletion observation.

Each card separates source inspection from limited replay evidence in `extensions`. The replay
artifact is `experiments/n8n-claw-memory/evidence/observations.json`, SHA-256
`88039348d8963431af9a229a17c46812ddb920534c70d1d7dcc80b0bb73d6ae7`; its `passed` field means the
observation matched that harness version's expectation, not that the behavior was safe.

## Scope and current B behavior

The cards use the existing `ScenarioCard` 0.4.0 contract and can be loaded from this directory.
Current keyword retrieval filters on task, runtime profile, and node type, then searches card title
and summary for changed field names. The summaries therefore include `jsCode` and `description`;
that match only ranks evidence for review. It does not assess ownership or safety. Current retrieval
does not search the extension evidence, impact paths, or policy bindings.

With the local test catalog stub matching the proposed template, both cards appear for both
generated A comparison pairs. Retrieval does not use the exact source revision or case ID to choose
between them, so the two hits are evidence candidates rather than a version-specific decision.
Keep that limitation visible until the B search context can use the exact source/path evidence.

`runtime_profile=core-local-v1` is the temporary A-analysis adapter profile, not a verified
n8n-claw deployment profile. The `n8n-claw-memory-static-scan@0.1.0-proposed` template reference
is not registered in the current B/C catalog. Artifact kinds are empty until the C scan contract is
connected. This change verifies card loading and retrieval only; it does not claim a generated C
plan, registered static scan, real database behavior, full n8n plus LLM execution, or end-to-end
A→B→C integration.

The cards make no CVE or CWE assignment. They cover direct `memory_long` row deletion in the
cited revisions; optional entity/relation cascade, actual service-key permissions, row-level
security, caller authentication, and production deployment remain outside the available evidence.

For a local A reproduction, follow `experiments/n8n-claw-a/README.md`. The source workflows and
analysis output contain upstream code and belong in ignored `var/` only. The checked-in card search
test uses a minimal in-memory context with the same task, profile, tool node type, and changed
`jsCode`/`description` fields, so tests do not download or redistribute upstream files.
