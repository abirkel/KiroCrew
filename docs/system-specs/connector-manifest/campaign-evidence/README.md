# Campaign evidence

A committed, distilled extract of the connector campaign's evidence-catalog
artifact, mirrored here so a `source.snapshot_ref` citing it resolves for a
reader who has only this repository checked out — see
[connector-capability-manifest.md](../../modules/connector-capability-manifest.md)'s
"Resolved this round (W00-S2)" paragraph on `source.snapshot_ref`'s resolution
contract for the rule this directory exists to satisfy.

## What is here

- [`catalog-evidence.json`](catalog-evidence.json) — a **distilled extract**
  of the campaign's evidence catalog: the operations/gaps/required-acceptance
  reconciliation counts, the evidence-tier distributions, and the full
  `shared_contracts` table (all 72 `contract_id` records across the
  `AUTH`/`GOV`/`RUN`/`KB`/`ACL`/`DATA`/`UX`/`SURF`/`OPS` families). Authoritative
  source for the 273-operation reconciliation
  (`services[].operations[]` (232) + `services[].gaps[].demoted_operations_full_record[]`
  (40) + the one contract-attachment operation (1) = 273) and for the 247-entry
  `required_acceptance_index`.

This is deliberately **not** the campaign's full working catalog. The full
per-service, per-operation research catalog (`services[]`, `sources[]`,
`scenario_preconditions`, `performance_preconditions`, `blocked_capabilities`,
`traceability_gaps`, `contradictions`) is NOT mirrored: nothing in this
repository's validator, tests, or spec text consumes it — only the counts,
tiers, and the shared-contract table itself are cited. Each `shared_contracts`
entry keeps its own `contract_id`/`title`/`description`/`evidence`/`gaps` —
the content that actually grounds the requirement, independent of the
campaign's own private task brief — but no longer carries a
`mission_brief_clause` field quoting that private brief's paragraphs
verbatim: nothing in this repository ever read that field, and it cited a
document with no in-repo home. The `meta` block similarly no longer narrates
the correction rounds that produced these counts (which clause mapping got
fixed in which pass, which earlier field was found inconsistent and
replaced) — that narrative described how the private catalog was assembled
over multiple rounds, not what this repository's validator, tests, or spec
require, and its removal changes no count: the 273-operation and 72-`contract_id`
totals, and every entry inside `required_acceptance_index`, are exactly as
before. Two earlier versions of this file mirrored progressively more of the
campaign's own working artifact and internal process — the full ~1MB/17k-line
catalog verbatim, then a first extract that still carried session
identifiers, private workspace paths, and the private mission-brief
citations/correction narrative above; both partial trims are superseded by
this version.

`contract-and-dag.md` (the campaign's own DAG/architecture-contract working
document) is deliberately **not** mirrored here: its work-stream DAG and
contract-family index duplicate content this repository's own
[connector-capability-manifest.md](../../modules/connector-capability-manifest.md)
already states authoritatively (that spec's numbering section is explicit
that it governs on any conflict), and the rest of that document was
campaign-internal architecture planning and round-by-round progress
narrative with no in-repo consumer.

`code-audit.json` (the campaign's audit of the existing connector-relevant
surface) is likewise deliberately NOT mirrored here: this PR's own validator,
tests, and spec do not consume it — a future round that actually needs it
resolvable mirrors it (or the specific facts it needs) in that round's own
commit, per this document tree's owning-spec rule below. Mirroring a file
only a sibling document's PROSE cites, with no validator or manifest-entry
`snapshot_ref` consuming it, is exactly the kind of forward-looking "we'll
want this later" this directory's own resolvability rule is not meant to
justify.

## What this is not

A live sync target, and not the campaign's full working record. This file is
a **distilled copy as of the commit that added or last refreshed it** — check
this directory's own git history for when the campaign's shared understanding
of its evidence changed, not the campaign's own private working directory,
which keeps evolving independently. A future round that needs a fresher
extract, or needs a specific operation record resolvable in-repo, re-copies
or re-derives just that content here in the same commit as whatever
manifest-entry change depends on it, per this document tree's owning-spec
rule.
