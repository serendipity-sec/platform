# n8n-claw Memory Delete knowledge cards

These team-authored cards summarize exact upstream revisions and the evidence recorded for issue #75. The upstream workflow has no separately verified license file, so the cards retain commit URLs, source sizes, and SHA-256 values only. They do not copy the workflow, JavaScript, or author tests.

## Cards

- `memory-delete-owner-check-gap.json` covers feature-add commit
  `63a7f2ee9c9e42fc6a3c4527fa2a7b7f12859377`. Source inspection found no requester, owner, scope,
  or admin comparison before the direct `memory_long` delete. The selected-JavaScript synthetic
  replay observed a foreign synthetic row being deleted. The separate Semgrep tool-fit summary
  reported a Memory Delete finding for its `feature` version.
- `memory-delete-owner-check-first-fix.json` covers first-fix commit
  `eb6d7bc185eaecdf96885b3e17ef1f8dac93a852`, compared with
  `4d1b1b748cdc76f6c61abb7c40ee8b01b560ef44`. Source inspection found the selected-row,
  caller-ID, and admin-list checks. The selected-JavaScript synthetic replay denied the foreign
  personal row and allowed the tested owner and admin cases. The Semgrep tool-fit summary
  completed with no findings for its `first-fix` version.

## Evidence types and execution status

Each claim has `evidence_refs` to a pinned source artifact, observation, or static summary record.
The replay and static selectors are pinned to supporting artifact records where those records are
available. Source-code claim locations remain unverified because the upstream bytes are not present
in this checkout. The evidence types remain separate:

- **Source inspection** covers the exact upstream commit and workflow hash named by each card.
- **Static analysis** references
  `experiments/n8n-claw-c/evidence/semgrep-summary.json`, SHA-256
  `afa1ea124fc82d6742f33c0408fd4728e6be49aa01765cd3f21605c42c89fd60`. The run used Semgrep
  1.181.0 and `flowdelta.n8n.agent-tool-write-without-ownership-check@0.4.0`, with
  `basis=STATIC_CONDITION` and `security_outcome=null`. The feature result includes findings for
  Memory Delete and Memory Update; the gap card selects only Memory Delete. The first-fix result
  has zero findings, including no Memory Delete finding. Zero findings are not proof of safety.
  The scan's coverage is limited: unsupported `toolWorkflow` and `telegramTool` nodes are listed
  in the report.
- **Selected-JavaScript synthetic replay** references
  `experiments/n8n-claw-memory/evidence/observations.json`, SHA-256
  `88039348d8963431af9a229a17c46812ddb920534c70d1d7dcc80b0bb73d6ae7`. It ran the selected
  original tool JavaScript against synthetic caller context and a synthetic HTTP receiver. The
  recorded selected-node replay status is `COMPLETED`.
- **Full n8n plus LLM agent execution: `NOT_RUN`. Real database execution: `NOT_RUN`.** The
  replay did not reproduce a deployed workflow or verify actual database permissions.

### Mapping to technology-neutral evidence layers

| General evidence layer | This case's n8n-specific record | Status and boundary |
|---|---|---|
| Source inspection | `source` entry in each card's `flowdelta.case_evidence.evidence` map: upstream workflow commit, path, and file SHA-256 | Pinned source revision identified. The source bytes are not checked into this checkout, so source-code claim locations cannot be independently inspected here. |
| Static analysis | `static_analysis` entry referencing `experiments/n8n-claw-c/evidence/semgrep-summary.json` | `SUCCEEDED` with Semgrep 1.181.0 and the recorded rule version. Only scanned node IDs are covered; unsupported node types remain listed, and `security_outcome` is null. |
| Bounded/component replay | `selected_tool_js_synthetic_replay` and `observations.json` | `COMPLETED` for selected original tool JavaScript against synthetic caller context and a synthetic HTTP receiver. This is component replay, not a deployed workflow or actual service effect. |
| Integrated workflow/system execution | `full_n8n_llm_agent` | `NOT_RUN`; the full n8n plus LLM path was not executed. |
| Backing-service effects | `real_database` | `NOT_RUN`; no actual database or its permissions were exercised. The synthetic receiver observation remains part of bounded replay. |

For each relevant layer, record status, scope, tool/configuration versions, artifact reference,
coverage, and limits; leave unavailable details explicit. A layer that does not apply can be omitted;
one that was not executed is `NOT_RUN`. Claims stay within the recorded layer and its boundary.

The observation artifact's `passed` flag means its expected harness observation matched; it does
not mean the behavior was safe. The cards retain `cve: NOT_IDENTIFIED`: the pinned workflow
revisions and limited replay reviewed on 2026-10-29 contain no CVE ID. No matching repository or
package advisory identity, package ecosystem/version, or product/version is established, so
GHSA/OSV/NVD applicability lookup was not performed and no CVE is claimed. No CWE is identified
either. This records ID discovery only. Each card separately records
`cve_applicability.status=UNKNOWN`, scoped to advisory applicability for the pinned workflow in an
assessed built/deployed runtime, because the evidence does not identify an exact package/product
version in that runtime or map the source commit to it. `runtime_profile=core-local-v1` is the
temporary analysis adapter profile, and `node_types` identifies an n8n node class; neither is the
runtime's installed package/product version. A workflow commit alone is not a runtime identity.

## Current B behavior and C registration

The cards use the `ScenarioCard` 0.4.0 contract and can be loaded from this directory. The contract's
generic `extensions` field does not enforce the semantics of
`extensions.flowdelta.case_evidence.claims[].evidence_refs`: it does not resolve a claim's key only
within that card's evidence map, check artifact hashes, or verify that a selector points to
supporting content. Current `load_cards()` validates the card structure, duplicate card versions,
and an optional `source_ref`; it does not resolve these extension references or require human review
and promotion. Those semantic evidence-reference checks belong to future #82 draft validation.

The current keyword retrieval filters on task, runtime profile, and node type, then searches card
title and summary for changed field names. The summaries include `jsCode` and `description`; a match
only ranks evidence for review. It does not assess ownership or safety. Retrieval does not search
the extension evidence, impact paths, or policy bindings.

With the local test catalog stub matching the proposed template, both cards appear for both
generated A comparison pairs. Retrieval does not use the exact source revision or case ID, so the
two hits are evidence candidates rather than a version-specific decision.

The current `main` [catalog](../../../examples/static-scan/catalog.json) registers the separate
C-side template `static-agent-tool-ownership@0.1.0`. That does not register the cards' proposed
`n8n-claw-memory-static-scan@0.1.0-proposed` reference; the cards retain
`template_status=PROPOSED_NOT_REGISTERED`. B-to-C plan-to-static-scan integration remains
incomplete under #78. These cards and the standalone tool-fit report are not B-generated C-plan
execution results.

`runtime_profile=core-local-v1` is the temporary A-analysis adapter profile, not a verified
n8n-claw deployment profile. The cards still declare `required_artifact_kinds: []`; the standalone
C catalog is not wired into B plan generation for these cards.
The cards cover direct `memory_long` row deletion only. Optional entity/relation cascade, actual
service-key permissions, row-level security, caller authentication, and production deployment
remain outside the available evidence.

## Related local criteria and evidence docs

- [C static-scan contract, catalog, and result boundaries](../../../docs/STATIC_SCAN.md)
- [Semgrep tool-fit experiment and version comparisons](../../../experiments/n8n-claw-c/README.md)
- [Selected-JavaScript replay method and limits](../../../experiments/n8n-claw-memory/README.md)
- [B knowledge operations and export boundary](../../../docs/KNOWLEDGE_OPERATIONS.md)

For a local A reproduction, follow [`experiments/n8n-claw-a/README.md`](../../../experiments/n8n-claw-a/README.md). Source workflows and analysis output contain upstream code and belong in ignored `var/` only. The checked-in card search test uses an in-memory context with the same task, profile, tool node type, and changed `jsCode`/`description` fields; tests do not download or redistribute upstream files.
