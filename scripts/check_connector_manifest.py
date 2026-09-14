#!/usr/bin/env python3
"""check_connector_manifest.py — validate connector-campaign manifest entries.

Enforces the field-level schema in
``docs/system-specs/modules/connector-capability-manifest.md`` against every
manifest entry, ``ConformanceRun``, and ``EvidenceReceipt`` record actually
committed to this repository. This is the W00-S2 validator: the spec itself
ships no runnable checker (deliberately — see that document's "What this
spec deliberately does not contain"), so this script is the first thing that
turns the spec's prose rules into a pass/fail gate a CI job can run.

## What this checks, structurally, per the spec

- Every manifest entry (one JSON file per operation, see the spec's
  "Resolved this round (W00-S2)" artifact-format paragraph) has the full
  required field set, correct types, and every required-when relationship
  the spec states (``pagination`` required iff ``operation_kind`` is
  ``list``/``search``; ``retry`` required iff ``effect`` is write-shaped;
  ``verification_contract`` non-null from ``code_complete`` on; the per-rung
  checklist; the three-way ``verification_contract`` <-> ``ConformanceRun``
  <-> ``EvidenceReceipt`` equality; the ``evidence_by_mode_surface_and_auth``
  totality rule once ``status`` leaves ``planned``; the per-cell four-
  coordinate equality against the run it cites).
- Every ``source.snapshot_ref`` resolves per the spec's now-fixed contract:
  an ``https://`` URL for ``official_docs``/``format_spec``, or an in-repo
  path that actually exists on disk for ``repo_path`` /
  ``search_snippet_corroborated`` / ``user_stated`` / ``not_yet_sourced``
  (except the ``not_yet_sourced`` placeholder itself, which is exempt by
  design — see the spec's own carve-out).
- Cross-file consistency: every ``ConformanceRun``/``EvidenceReceipt``
  reference a manifest entry makes actually resolves to a record that
  exists, and the referenced record's own back-pointers agree (the
  three-way equality above), never assumed from field presence alone.
- Denominator/count invariants the campaign's evidence catalog states
  (``services[].operations[]`` + ``gaps[].demoted_operations_full_record[]``
  + the one contract-attachment operation == 273; the 72 ``contract_id``
  total) are NOT cross-checked by this validator against
  ``catalog-evidence.json`` — this gate validates
  manifest *entries* against the manifest *schema*; the campaign evidence
  catalog's own counts are that catalog's own responsibility to keep
  internally consistent (see the spec's "What 'the required range' means"),
  and duplicating that cross-check here would create a second place those
  invariants could drift out of sync with the catalog's own reconciliation
  math. A future round that wants this specific cross-check built is a
  separate, explicitly-scoped addition, not an implicit consequence of
  mirroring the catalog's files into this repo.

## What this deliberately does not check

- Liveness of an ``https://`` `snapshot_ref` (no network call — this is a
  static validator; see the spec's own "does not contain" list).
- Whether a `ConformanceRun`'s `verdict: pass` is actually TRUE (that is the
  conformance runner's job, a separate, not-yet-built round; this validator
  only checks that the schema's structural relationships hold given
  whatever verdict is recorded).
- Anything about the runner or discovery protocol's *implementation* — only
  the manifest-entry / `ConformanceRun` / `EvidenceReceipt` *schema*.

## Usage

    python3 scripts/check_connector_manifest.py          # validate every entry
    python3 scripts/check_connector_manifest.py --test   # self-test the rules
    python3 scripts/check_connector_manifest.py --entry docs/system-specs/connector-manifest/entries/github/gh_search_repositories.json
                                                  # validate one entry file
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_ROOT = os.path.join(REPO_ROOT, "docs", "system-specs", "connector-manifest")
ENTRIES_ROOT = os.path.join(MANIFEST_ROOT, "entries")
RUNS_ROOT = os.path.join(MANIFEST_ROOT, "runs")
RECEIPTS_ROOT = os.path.join(MANIFEST_ROOT, "receipts")
# The mirrored campaign evidence-catalog extract. This gate cross-checks its
# reconciliation counts (the load-bearing denominators the campaign fixed:
# 272 operations across tiers + 1 contract-attachment = 273; 72 shared-contract
# records across the 9 families; 247 required-acceptance-index entries) against
# the file's OWN arrays and sub-counts, so the extract has a real machine
# consumer in this repo and the denominators are held machine-checked, not by
# a hardcoded literal that could drift (this is the count cross-check the
# format decision's Design review asked to read from JSON, and the consumer
# that makes mirroring the extract load-bearing rather than prose-only).
CATALOG_EVIDENCE = os.path.join(MANIFEST_ROOT, "campaign-evidence", "catalog-evidence.json")
# The 9 shared-contract families whose records sum to the 72 contract_id count
# (the 10th top-level key is a note, not a family).
_SHARED_CONTRACT_FAMILIES = ("AUTH", "GOV", "RUN", "KB", "ACL", "DATA", "UX", "SURF", "OPS")
_EXPECTED_SHARED_CONTRACT_COUNT = 72
_EXPECTED_TOTAL_OPS_WITH_ATTACHMENT = 273
_EXPECTED_REQUIRED_ACCEPTANCE_INDEX = 247
# The authoritative governance SCOPE_CATALOG lives as a Dict literal in this
# module's source. A non-null policy.*_scope must name one of its keys. The
# validator reads those keys from this file via AST (never imports it — it
# pulls a heavy kiro_crew runtime dependency chain the stdlib-only gate has
# no environment for — and never copies them into a second list here).
GOVERNANCE_PY = os.path.join(REPO_ROOT, "src", "kiro_crew", "platform", "governance.py")

# ---------------------------------------------------------------------------
# Closed vocabularies, copied verbatim from the spec. A validator checks
# membership against THESE lists, never against anything outside this file
# or the spec it mirrors — see the spec's own "closed set" language for each.
# ---------------------------------------------------------------------------

SERVICE_IDS = frozenset(
    {
        "github",
        "gmail",
        "google_drive",
        "sharepoint",
        "outlook",
        "onedrive",
        "onenote",
        "teams",
        "excel_shared_engine",
        "office_documents",
        "slack",
        "asana",
        "salesforce",
        "zoom",
    }
)

CATEGORIES = frozenset({"baseline_alignment", "production_requirement", "user_extension"})

SOURCE_STATUSES = frozenset({"user_required", "official_baseline", "unverified"})

SOURCE_KINDS = frozenset(
    {
        "official_docs",
        "repo_path",
        "format_spec",
        "search_snippet_corroborated",
        "user_stated",
        "not_yet_sourced",
    }
)

# source_kind values whose snapshot_ref must be a resolvable in-repo path
# (excluding the not_yet_sourced placeholder, handled separately).
_REPO_PATH_SOURCE_KINDS = frozenset({"repo_path", "search_snippet_corroborated", "user_stated"})
_URL_SOURCE_KINDS = frozenset({"official_docs", "format_spec"})

EFFECTS = frozenset({"read", "write", "delete", "share", "external_send", "admin", "billable"})

# effect values whose retry field is required (every effect except read and
# billable — the spec's own required-when wording for `retry`).
_WRITE_SHAPED_EFFECTS = frozenset({"write", "delete", "share", "external_send", "admin"})

IDEMPOTENCY_CLASSES = frozenset(
    {
        "base_sha_guard",
        "generate_ids_preallocation",
        "external_id_upsert",
        "none_verify_by_readback",
    }
)

OPERATION_KINDS = frozenset({"single_fetch", "list", "search", "mutation", "stream"})
_PAGINATION_REQUIRED_KINDS = frozenset({"list", "search"})

# The eight-value status ladder, in rung order (index = rung position).
STATUS_LADDER = (
    "planned",
    "implementing",
    "code_complete",
    "contract_verified",
    "live_verified",
    "merged",
    "release_verified",
)
STATUS_VALUES = frozenset(STATUS_LADDER) | {"blocked"}
# last_reached_status's legal values exclude `blocked` itself.
LAST_REACHED_VALUES = frozenset(STATUS_LADDER)

# Per-rung required non-null/non-empty fields, cumulative down the ladder,
# copied verbatim from the spec's own table.
_RUNG_INDEX = {name: i for i, name in enumerate(STATUS_LADDER)}

CONFORMANCE_VERDICTS = frozenset({"pass", "fail", "inconclusive"})

CLEANUP_STATUSES = frozenset({"not_applicable", "confirmed", "not_automatable", "pending"})

_ADAPTER_SENTINEL = {"module_ref": "UNASSIGNED", "version": "0.0.0-unassigned"}


# ---------------------------------------------------------------------------
# Result plumbing
# ---------------------------------------------------------------------------


def _member_of(value: Any, allowed: frozenset[str]) -> bool:
    """Safe replacement for ``value in allowed`` when ``value`` comes straight
    from untrusted, decoded JSON. A ``frozenset[str]`` membership test hashes
    its argument, and an unhashable JSON type (a list or a dict — JSON has no
    other unhashable shapes) raises ``TypeError`` instead of cleanly
    returning ``False``. A manifest entry whose ``category`` is ``[]``
    instead of a string must fail validation with a Finding, not crash the
    whole gate. Returns ``False`` for any non-``str`` value rather than
    raising."""
    return isinstance(value, str) and value in allowed


def _is_resolvable_https_url(value: Any) -> bool:
    """A ``str.startswith("https://")`` check alone accepts the bare literal
    ``"https://"`` (and any hostless variant, e.g. ``"https:///x"``) as a
    valid citation, because the prefix test says nothing about whether a
    host follows it — an unresolvable citation would then be certified as
    if it were a real, reachable URL. Parses the string and additionally
    requires a non-empty ``netloc`` (the host/authority component)."""
    if not isinstance(value, str) or not value.startswith("https://"):
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        # A malformed URL (e.g. one containing a NUL byte) is not resolvable
        # either — treat the parse failure itself as "not a resolvable URL"
        # rather than letting it propagate and crash the gate.
        return False
    # Require a real host, not merely a non-empty netloc: a netloc like
    # "user@" or ":443" has no hostname, so a citation with no reachable host
    # would otherwise be certified as resolvable. parsed.hostname is None in
    # exactly those hostless cases.
    return bool(parsed.hostname)


def _safe_realpath(path: str) -> str | None:
    """``os.path.realpath`` raises ``ValueError`` (uncaught anywhere in this
    file) when ``path`` contains an embedded NUL byte — legal inside a JSON
    string, and therefore a writer-producible manifest value. A single
    malformed entry must fail closed with a Finding, never terminate the
    whole validator run. Returns ``None`` on any such failure."""
    try:
        return os.path.realpath(path)
    except ValueError:
        return None


@dataclass
class Finding:
    """One concrete validation failure, always naming the entry and field."""

    entry_ref: str
    field: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return f"{self.entry_ref}: [{self.field}] {self.message}"


@dataclass
class ValidationResult:
    ok: bool
    findings: list[Finding] = field(default_factory=list)

    def add(self, entry_ref: str, field_name: str, message: str) -> None:
        self.ok = False
        self.findings.append(Finding(entry_ref, field_name, message))


# ---------------------------------------------------------------------------
# JSONL helpers for ConformanceRun / EvidenceReceipt lookup
# ---------------------------------------------------------------------------


def _load_jsonl(path: str, id_field: str | None = None) -> list[dict[str, Any]]:
    """Load a JSON Lines file, validating each record's basic shape.

    A record that decodes as valid JSON but is not an object (a bare
    scalar, a list, ``null``) would crash ``_find_by_id``'s
    ``rec.get(id_field)`` with ``AttributeError`` on its first lookup —
    malformed evidence would crash the whole gate instead of failing the
    one entry that referenced it. Every record is checked to be a JSON
    object before being accepted. When ``id_field`` is given
    (``"run_id"``/``"receipt_id"``), a duplicate value is also rejected
    here, before any lookup happens, rather than silently first-matching and certifying an ambiguous
    evidence graph as if it were unambiguous.
    """
    if not os.path.exists(path):
        return []
    records: list[dict[str, Any]] = []
    seen_ids: dict[Any, int] = {}
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"{path}:{line_no}: expected a JSON object per line, got "
                    f"{type(record).__name__}"
                )
            if id_field is not None:
                record_id = record.get(id_field)
                if record_id is not None and not isinstance(record_id, str):
                    raise ValueError(
                        f"{path}:{line_no}: {id_field}={record_id!r} must be a string "
                        f"to be used as an evidence-record identity, got "
                        f"{type(record_id).__name__}"
                    )
                if record_id is not None and record_id in seen_ids:
                    raise ValueError(
                        f"{path}:{line_no}: duplicate {id_field}={record_id!r} "
                        f"(first seen on line {seen_ids[record_id]}) — ambiguous "
                        "evidence graph, refusing to first-match"
                    )
                if record_id is not None:
                    seen_ids[record_id] = line_no
            records.append(record)
    return records


def _find_by_id(records: list[dict[str, Any]], id_field: str, value: str) -> dict[str, Any] | None:
    for rec in records:
        if rec.get(id_field) == value:
            return rec
    return None


def _runs_for_service(service_id: str) -> list[dict[str, Any]]:
    return _load_jsonl(os.path.join(RUNS_ROOT, f"{service_id}.jsonl"), id_field="run_id")


def _receipts_for_service(service_id: str) -> list[dict[str, Any]]:
    return _load_jsonl(os.path.join(RECEIPTS_ROOT, f"{service_id}.jsonl"), id_field="receipt_id")


_SCOPE_CATALOG_CACHE: frozenset[str] | None = None
_SCOPE_CATALOG_ERROR: str | None = None
_SCOPE_CATALOG_LOADED = False


def _governance_scope_catalog() -> frozenset[str] | None:
    """Return the set of valid governance scope names — the string-literal
    keys of the SCOPE_CATALOG dict in src/kiro_crew/platform/governance.py —
    read via AST from that file's source.

    Why AST-read and NOT ``import kiro_crew.platform.governance``: this
    validator is stdlib-only and its CI gate runs it as a bare
    ``python3 scripts/check_connector_manifest.py`` on a plain checkout, with
    no installed ``kiro_crew`` package and no PYTHONPATH; governance.py's own
    module-level imports (kiro_crew.config.paths, .platform.admission,
    kiro_crew.sel, ...) would raise ImportError. AST-reading the literal keeps
    governance.py the SINGLE authoritative source (no second copied list here)
    while needing none of that runtime. DO NOT "fix" this into an import — it
    would break the gate in CI. If governance.py ever stops being a static
    literal, that is a fail-closed condition (see _SCOPE_CATALOG_ERROR), not a
    reason to import.

    Returns the key set, or None when the catalog cannot be statically read.
    On None, _SCOPE_CATALOG_ERROR holds a human-readable reason; the whole-tree
    scan turns that into a hard failure (see _check_scope_catalog_readable)
    so an unreadable catalog fails the gate LOUDLY even when no manifest entry
    happens to reference a non-null scope this run — never a silent skip.
    """
    global _SCOPE_CATALOG_CACHE, _SCOPE_CATALOG_ERROR, _SCOPE_CATALOG_LOADED
    if _SCOPE_CATALOG_LOADED:
        return _SCOPE_CATALOG_CACHE
    _SCOPE_CATALOG_LOADED = True
    # Case 1: file missing / unreadable / not parseable.
    try:
        with open(GOVERNANCE_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=GOVERNANCE_PY)
    except OSError as exc:
        _SCOPE_CATALOG_ERROR = f"{GOVERNANCE_PY} could not be read ({exc.__class__.__name__})"
        _SCOPE_CATALOG_CACHE = None
        return None
    except SyntaxError as exc:
        _SCOPE_CATALOG_ERROR = f"{GOVERNANCE_PY} did not parse ({exc.__class__.__name__})"
        _SCOPE_CATALOG_CACHE = None
        return None
    for node in ast.walk(tree):
        target_names: list[str] = []
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value = node.value
        elif isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        if "SCOPE_CATALOG" not in target_names:
            continue
        # Case 3: found the assignment, but it is no longer a statically
        # evaluable dict literal (someone changed it to a comprehension, a
        # function call, a merge, ...). Fail closed — we can no longer read
        # the authoritative membership set without executing code.
        if not isinstance(value, ast.Dict):
            _SCOPE_CATALOG_ERROR = (
                f"SCOPE_CATALOG in {GOVERNANCE_PY} is no longer a static dict literal "
                f"(got {type(value).__name__}) — this reader cannot extract its keys without "
                "executing governance.py; update this reader deliberately, do not import"
            )
            _SCOPE_CATALOG_CACHE = None
            return None
        keys: set[str] = set()
        for k in value.keys:
            # A non-literal key (a **splat, a computed key) means the literal is
            # not the simple mapping this reader assumes; fail closed rather
            # than accept a partial view.
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
            else:
                _SCOPE_CATALOG_ERROR = (
                    f"SCOPE_CATALOG in {GOVERNANCE_PY} has a non-string-literal key "
                    "(a splat or computed key) — this reader cannot extract a complete "
                    "key set statically; fail closed"
                )
                _SCOPE_CATALOG_CACHE = None
                return None
        if not keys:
            _SCOPE_CATALOG_ERROR = (
                f"SCOPE_CATALOG in {GOVERNANCE_PY} parsed to an EMPTY key set — refusing to "
                "treat 'no members' as valid (it would make every scope value fail or the "
                "check meaningless); fail closed"
            )
            _SCOPE_CATALOG_CACHE = None
            return None
        _SCOPE_CATALOG_CACHE = frozenset(keys)
        return _SCOPE_CATALOG_CACHE
    # Case 2: file parsed, but no SCOPE_CATALOG assignment node found at all.
    _SCOPE_CATALOG_ERROR = (
        f"no SCOPE_CATALOG assignment found in {GOVERNANCE_PY} — the authoritative "
        "governance scope catalog could not be located; fail closed"
    )
    _SCOPE_CATALOG_CACHE = None
    return None


def _check_scope_catalog_readable(result: ValidationResult) -> None:
    """Whole-tree invariant: the authoritative SCOPE_CATALOG MUST be statically
    readable, independent of whether any manifest entry references a non-null
    policy scope this run. Without this, deleting/breaking governance.py's
    catalog would silently disable membership validation whenever every entry's
    policy scopes happen to be null (same shape as the 'delete the file to turn
    off the invariant' failure mode). Emits a hard finding otherwise."""
    if _governance_scope_catalog() is None:
        result.add(
            "<scope-catalog>",
            "governance.SCOPE_CATALOG",
            _SCOPE_CATALOG_ERROR
            or f"the authoritative SCOPE_CATALOG in {GOVERNANCE_PY} is unreadable — failing closed",
        )


def _is_strict_int(value: Any) -> bool:
    """True only for a real integer, NOT a bool. In Python bool is a subclass
    of int, but a JSON boolean is not a JSON integer — a count field holding
    `true` must not satisfy an integer/count requirement."""
    return isinstance(value, int) and not isinstance(value, bool)


def _check_catalog_evidence_counts(result: ValidationResult) -> None:
    """Whole-tree cross-check of the mirrored campaign evidence-catalog extract
    (catalog-evidence.json). This is what makes mirroring that file load-bearing
    rather than prose-only (FP review), and it reads the reconciliation counts
    FROM the JSON rather than a hardcoded literal (Design review), so only one
    place can drift.

    It verifies the file is internally self-consistent AND holds the campaign's
    fixed denominators — the counts must never silently shrink:
      * the 9 shared-contract families' records sum to 72 contract_id records;
      * operations-by-tier sums to total_operations_all_tiers_combined, and
        that + the contract-attachment count == the 273 all-tiers-with-attachment
        total;
      * required_acceptance_index has 247 entries, matching its own summary.
    A missing/unreadable file, or any count that does not reconcile, is a hard
    finding (fail-closed) — the extract cannot silently lose the denominators.
    """
    ref = "<catalog-evidence>"
    try:
        with open(CATALOG_EVIDENCE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        result.add(
            ref,
            "catalog-evidence.json",
            f"{CATALOG_EVIDENCE} could not be read/parsed ({exc.__class__.__name__}) — "
            "the required-count denominators have no verifiable home; failing closed",
        )
        return
    if not isinstance(data, dict):
        result.add(ref, "catalog-evidence.json", "top-level value must be an object")
        return

    # 72 shared-contract records across the 9 families.
    shared = data.get("shared_contracts")
    if not isinstance(shared, dict):
        result.add(ref, "shared_contracts", "required object (per-family lists) is missing")
    else:
        total_contracts = 0
        for fam in _SHARED_CONTRACT_FAMILIES:
            fam_list = shared.get(fam)
            if not isinstance(fam_list, list):
                result.add(
                    ref, f"shared_contracts.{fam}", "required family list is missing or not a list"
                )
            else:
                total_contracts += len(fam_list)
        if total_contracts != _EXPECTED_SHARED_CONTRACT_COUNT:
            result.add(
                ref,
                "shared_contracts",
                f"the 9 contract families hold {total_contracts} records, expected "
                f"{_EXPECTED_SHARED_CONTRACT_COUNT} (the contract_id denominator must not shrink)",
            )

    cov = data.get("coverage")
    if not isinstance(cov, dict):
        result.add(ref, "coverage", "required object is missing")
    else:
        # tier breakdown sums to total_operations_all_tiers_combined. Use a
        # STRICT integer test (bool is a subclass of int in Python, but a JSON
        # boolean is not a JSON integer — `true` must not satisfy a count).
        tiers = cov.get("operations_by_evidence_tier")
        total_all_tiers = cov.get("total_operations_all_tiers_combined")
        if isinstance(tiers, dict) and all(_is_strict_int(v) for v in tiers.values()):
            tier_sum = sum(tiers.values())
            if tier_sum != total_all_tiers:
                result.add(
                    ref,
                    "coverage.operations_by_evidence_tier",
                    f"tiers sum to {tier_sum} but total_operations_all_tiers_combined is "
                    f"{total_all_tiers!r} — reconciliation broken",
                )
        else:
            result.add(ref, "coverage.operations_by_evidence_tier", "missing or non-integer tiers")
        # total + contract-attachment == 273. Both inputs must be present
        # integers — a missing total_operations_all_tiers_combined or a missing
        # contract_attachment_operations.count must FAIL CLOSED, not skip the
        # reconciliation and let the inclusive total pass unchecked.
        attach = cov.get("contract_attachment_operations")
        attach_count = attach.get("count") if isinstance(attach, dict) else None
        incl = cov.get("total_operations_all_tiers_combined_including_contract_attachment")
        if not _is_strict_int(total_all_tiers):
            result.add(
                ref,
                "coverage.total_operations_all_tiers_combined",
                f"required integer is missing or non-integer ({total_all_tiers!r}) — the "
                "operations reconciliation cannot be verified; failing closed",
            )
        if not _is_strict_int(attach_count):
            result.add(
                ref,
                "coverage.contract_attachment_operations.count",
                f"required integer is missing or non-integer ({attach_count!r}) — the "
                "operations reconciliation cannot be verified; failing closed",
            )
        if (
            isinstance(total_all_tiers, int)
            and not isinstance(total_all_tiers, bool)
            and isinstance(attach_count, int)
            and not isinstance(attach_count, bool)
            and total_all_tiers + attach_count != incl
        ):
            result.add(
                ref,
                "coverage.total_operations_all_tiers_combined_including_contract_attachment",
                f"{total_all_tiers!r} + {attach_count!r} != {incl!r} — reconciliation broken",
            )
        if incl != _EXPECTED_TOTAL_OPS_WITH_ATTACHMENT:
            result.add(
                ref,
                "coverage.total_operations_all_tiers_combined_including_contract_attachment",
                f"is {incl!r}, expected {_EXPECTED_TOTAL_OPS_WITH_ATTACHMENT} "
                "(the operations denominator must not shrink)",
            )
        # required_acceptance_index has 247 entries, matching its own summary
        rai = data.get("required_acceptance_index")
        rai_len = len(rai) if isinstance(rai, list) else None
        summary = cov.get("required_acceptance_index_summary")
        declared = summary.get("total_entries") if isinstance(summary, dict) else None
        if rai_len != _EXPECTED_REQUIRED_ACCEPTANCE_INDEX:
            result.add(
                ref,
                "required_acceptance_index",
                f"has {rai_len!r} entries, expected {_EXPECTED_REQUIRED_ACCEPTANCE_INDEX} "
                "(the acceptance-index denominator must not shrink)",
            )
        if declared != rai_len:
            result.add(
                ref,
                "coverage.required_acceptance_index_summary.total_entries",
                f"declares {declared!r} but the array has {rai_len!r} entries — reconciliation "
                "broken",
            )


# ---------------------------------------------------------------------------
# Field-level checks
# ---------------------------------------------------------------------------


def _require(result: ValidationResult, ref: str, entry: dict[str, Any], field_name: str) -> Any:
    if field_name not in entry:
        result.add(ref, field_name, "required field is missing")
        return None
    return entry[field_name]


def _require_non_null(
    result: ValidationResult, ref: str, entry: dict[str, Any], field_name: str
) -> Any:
    """Like ``_require``, but additionally rejects a key that IS present
    with a JSON ``null`` value. A required field whose own spec-declared
    type never includes ``null`` (unlike ``verification_contract``,
    ``tested_sha``, ``merged_sha``, ``release_sha``, which are legitimately
    ``null`` at early rungs and are checked for non-null-ness downstream,
    conditional on rung) must not treat "key present, value null" as
    satisfying its own required-and-typed contract."""
    value = _require(result, ref, entry, field_name)
    if field_name in entry and value is None:
        result.add(ref, field_name, "required field is present but null")
        return None
    return value


def _check_enum(
    result: ValidationResult,
    ref: str,
    entry: dict[str, Any],
    field_name: str,
    allowed: frozenset[str],
) -> None:
    value = entry.get(field_name)
    if value is None:
        return
    if not _member_of(value, allowed):
        result.add(ref, field_name, f"{value!r} is not one of {sorted(allowed)}")


def _check_snapshot_ref(result: ValidationResult, ref: str, source: dict[str, Any]) -> None:
    kind = source.get("source_kind")
    snapshot_ref = source.get("snapshot_ref")
    if kind is None or snapshot_ref is None:
        return  # already flagged by required-field checks
    if kind == "not_yet_sourced":
        if not isinstance(snapshot_ref, str) or not snapshot_ref.strip():
            result.add(
                ref,
                "source.snapshot_ref",
                "not_yet_sourced requires an explicit placeholder string, not blank",
            )
        return
    if _member_of(kind, _URL_SOURCE_KINDS):
        if not _is_resolvable_https_url(snapshot_ref):
            result.add(
                ref,
                "source.snapshot_ref",
                f"source_kind={kind!r} requires an https:// URL with a real host, "
                f"got {snapshot_ref!r}",
            )
        return
    if _member_of(kind, _REPO_PATH_SOURCE_KINDS):
        if not isinstance(snapshot_ref, str) or not snapshot_ref.strip():
            result.add(
                ref,
                "source.snapshot_ref",
                f"source_kind={kind!r} requires a non-empty repo-relative path",
            )
            return
        if os.path.isabs(snapshot_ref):
            result.add(
                ref,
                "source.snapshot_ref",
                f"source_kind={kind!r} requires a path RELATIVE to the repo root, "
                f"not an absolute path ({snapshot_ref!r}) — an absolute path can point "
                "outside this repository (e.g. a private campaign workspace) and is "
                "exactly what this rule closes, per the spec's snapshot_ref resolution contract",
            )
            return
        abs_path = os.path.join(REPO_ROOT, snapshot_ref)
        real_repo_root = _safe_realpath(REPO_ROOT)
        real_abs_path = _safe_realpath(abs_path)
        if real_abs_path is None or real_repo_root is None:
            result.add(
                ref,
                "source.snapshot_ref",
                f"source_kind={kind!r}: {snapshot_ref!r} is not a resolvable path "
                "(contains a character the filesystem cannot resolve, e.g. an "
                "embedded NUL byte)",
            )
            return
        if os.path.commonpath([real_abs_path, real_repo_root]) != real_repo_root:
            result.add(
                ref,
                "source.snapshot_ref",
                f"source_kind={kind!r}: {snapshot_ref!r} resolves (after following "
                "any '..' segments and symlinks) to a real path OUTSIDE the repo "
                f"root ({real_abs_path!r} is not under {real_repo_root!r}) — a "
                "relative-looking path is not sufficient; the RESOLVED path must "
                "stay inside the repository",
            )
            return
        if not os.path.exists(abs_path):
            result.add(
                ref,
                "source.snapshot_ref",
                f"source_kind={kind!r} requires an in-repo path that exists; "
                f"{snapshot_ref!r} does not resolve under the repo root",
            )
        return
    result.add(ref, "source.source_kind", f"unhandled source_kind {kind!r}")


def _check_schema_ref(
    result: ValidationResult, ref: str, schema_field: str, schema: dict[str, Any]
) -> None:
    """`snapshot_ref` gets a real resolvability check
    (this file's own central rule — "the evidence a reader needs to audit a
    claim travels with the claim") but `input_schema.schema_ref` /
    `output_schema.schema_ref` were only checked for PRESENCE, never
    resolvability, which is the same defect class the spec's `snapshot_ref`
    contract exists to close one field over. Applies the identical two-shape
    rule: an `https://` URL, or an in-repo path whose REAL path (after
    resolving any `..` segments and symlinks) stays under the repo root and
    exists. Unlike `source.snapshot_ref`, `schema_ref` has no
    `not_yet_sourced`-style placeholder carve-out in the spec — a manifest
    entry is expected to know where its own input/output shape is defined
    from the moment it exists, even at `status: planned`."""
    schema_ref = schema.get("schema_ref")
    if not isinstance(schema_ref, str) or not schema_ref.strip():
        return  # already flagged by the required-sub-field check above
    field_name = f"{schema_field}.schema_ref"
    if schema_ref.startswith("https://"):
        if not _is_resolvable_https_url(schema_ref):
            result.add(
                ref,
                field_name,
                f"{schema_ref!r} starts with 'https://' but has no host — not a " "resolvable URL",
            )
        return
    if os.path.isabs(schema_ref):
        result.add(
            ref,
            field_name,
            f"{schema_ref!r} must be an https:// URL or a path RELATIVE to the "
            "repo root, not an absolute filesystem path",
        )
        return
    abs_path = os.path.join(REPO_ROOT, schema_ref.split("#", 1)[0])
    real_repo_root = _safe_realpath(REPO_ROOT)
    real_abs_path = _safe_realpath(abs_path)
    if real_abs_path is None or real_repo_root is None:
        result.add(
            ref,
            field_name,
            f"{schema_ref!r} is not a resolvable path (contains a character the "
            "filesystem cannot resolve, e.g. an embedded NUL byte)",
        )
        return
    if os.path.commonpath([real_abs_path, real_repo_root]) != real_repo_root:
        result.add(
            ref,
            field_name,
            f"{schema_ref!r} resolves (after following any '..' segments and "
            f"symlinks) to a real path OUTSIDE the repo root — the RESOLVED "
            "path must stay inside the repository",
        )
        return
    if not os.path.exists(abs_path):
        result.add(
            ref,
            field_name,
            f"{schema_ref!r} must be an https:// URL or an in-repo path that "
            "exists (an optional '#fragment' after the path is stripped "
            "before the existence check); it does not resolve under the "
            "repo root",
        )


def _check_source(result: ValidationResult, ref: str, entry: dict[str, Any]) -> None:
    source = entry.get("source")
    if not isinstance(source, dict):
        result.add(ref, "source", "required object field is missing or not an object")
        return
    for sub in ("source_kind", "source_id", "observed_at", "snapshot_ref"):
        if sub not in source:
            result.add(ref, f"source.{sub}", "required sub-field is missing")
    kind = source.get("source_kind")
    if kind is not None and not _member_of(kind, SOURCE_KINDS):
        result.add(ref, "source.source_kind", f"{kind!r} is not one of {sorted(SOURCE_KINDS)}")
        return
    # The spec mandates two source_kind -> source_status pairings (the other
    # four kinds are unconstrained authority-wise): user_stated pairs with
    # source_status user_required, and not_yet_sourced pairs with unverified
    # (spec's source_kind table, "Pairs with source_status: ..."). A
    # contradictory pairing (e.g. not_yet_sourced + official_baseline) is a
    # provenance error and is rejected here.
    _REQUIRED_STATUS_FOR_KIND = {
        "user_stated": "user_required",
        "not_yet_sourced": "unverified",
    }
    required_status = _REQUIRED_STATUS_FOR_KIND.get(kind) if isinstance(kind, str) else None
    if required_status is not None:
        actual_status = entry.get("source_status")
        if actual_status != required_status:
            result.add(
                ref,
                "source_status",
                f"source_kind={kind!r} requires source_status={required_status!r} "
                f"(spec pairing), got {actual_status!r}",
            )
    # Source scalar types (spec: source is {source_kind, source_id, observed_at,
    # snapshot_ref}). source_id and snapshot_ref are always real strings; a
    # null/blank/non-string value was accepted before. observed_at is null ONLY
    # for not_yet_sourced (checked below); for every other kind it is a real
    # timestamp string.
    source_id = source.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        result.add(ref, "source.source_id", "required non-empty string")
    if kind == "not_yet_sourced":
        if source.get("observed_at") is not None:
            result.add(
                ref, "source.observed_at", "must be null when source_kind is not_yet_sourced"
            )
    else:
        observed_at = source.get("observed_at")
        if not isinstance(observed_at, str) or not observed_at.strip():
            result.add(
                ref,
                "source.observed_at",
                f"required non-empty timestamp string when source_kind is {kind!r} "
                "(only not_yet_sourced may leave it null)",
            )
    _check_snapshot_ref(result, ref, source)


def _check_adapter(result: ValidationResult, ref: str, entry: dict[str, Any]) -> None:
    adapter = entry.get("adapter")
    if not isinstance(adapter, dict):
        result.add(ref, "adapter", "required object field is missing or not an object")
        return
    for sub in ("module_ref", "version"):
        sub_value = adapter.get(sub)
        if sub not in adapter:
            result.add(ref, f"adapter.{sub}", "required sub-field is missing")
        elif not isinstance(sub_value, str) or not sub_value.strip():
            # The spec types both adapter sub-fields as `string` (line 180),
            # and the sentinel pair is itself a pair of literal strings. A
            # numeric/blank value must be rejected; a key-presence-only check
            # is not sufficient.
            result.add(
                ref,
                f"adapter.{sub}",
                f"must be a non-empty string, got {type(sub_value).__name__}",
            )
    module_ref = adapter.get("module_ref")
    version = adapter.get("version")
    is_sentinel = (
        module_ref == _ADAPTER_SENTINEL["module_ref"] and version == _ADAPTER_SENTINEL["version"]
    )
    is_sentinel_partial = (module_ref == _ADAPTER_SENTINEL["module_ref"]) != (
        version == _ADAPTER_SENTINEL["version"]
    )
    if is_sentinel_partial:
        result.add(
            ref,
            "adapter",
            "module_ref/version sentinel pair must both be the exact UNASSIGNED "
            "sentinel or both be a real assignment, never one of each",
        )
    status = entry.get("status")
    if status == "planned" and not is_sentinel and not is_sentinel_partial:
        # planned entries are not required to be unassigned, but a real
        # module_ref that looks fictitious is out of scope for this
        # structural validator (no way to check a path exists without
        # resolving arbitrary code paths); no finding raised here.
        pass


def _check_retry(result: ValidationResult, ref: str, entry: dict[str, Any]) -> None:
    effect = entry.get("effect")
    retry = entry.get("retry")
    if _member_of(effect, _WRITE_SHAPED_EFFECTS):
        if retry is None:
            result.add(ref, "retry", f"required when effect={effect!r}, but is null/missing")
            return
        if not isinstance(retry, dict):
            result.add(ref, "retry", "must be an object")
            return
        idempotency_class = retry.get("idempotency_class")
        if not _member_of(idempotency_class, IDEMPOTENCY_CLASSES):
            result.add(
                ref,
                "retry.idempotency_class",
                f"{idempotency_class!r} is not one of {sorted(IDEMPOTENCY_CLASSES)}",
            )
        if "detail" not in retry:
            result.add(ref, "retry.detail", "required sub-field is missing")
    elif retry is not None:
        # effect is read or billable: retry is legal to omit, and this
        # validator does not forbid a stray value, but a non-null retry on
        # a read-only operation is almost certainly a copy-paste error.
        result.add(
            ref,
            "retry",
            f"present but effect={effect!r} does not require it "
            "(only write/delete/share/external_send/admin do) — remove it or "
            "confirm the effect value is correct",
        )


def _check_pagination(result: ValidationResult, ref: str, entry: dict[str, Any]) -> None:
    op_kind = entry.get("operation_kind")
    pagination = entry.get("pagination")
    if _member_of(op_kind, _PAGINATION_REQUIRED_KINDS):
        if not pagination:
            result.add(
                ref,
                "pagination",
                f"required when operation_kind={op_kind!r}, but is null/missing/empty",
            )


def _check_policy(result: ValidationResult, ref: str, entry: dict[str, Any]) -> None:
    policy = entry.get("policy")
    if not isinstance(policy, dict):
        result.add(ref, "policy", "required object field is missing or not an object")
        return
    for sub in (
        "platform_scope",
        "workspace_scope",
        "session_scope",
        "connection_scope",
        "provider_scope",
    ):
        if sub not in policy:
            result.add(
                ref, f"policy.{sub}", "required sub-field is missing (use null, not omission)"
            )
            continue
        value = policy[sub]
        if value is None:
            # null = no additional governance constraint on this layer. The
            # layer field NAME (platform_scope/.../provider_scope) is not
            # itself a catalog member; only a non-null VALUE names one.
            continue
        if not isinstance(value, str):
            result.add(ref, f"policy.{sub}", "must be a string or null")
            continue
        # A non-null policy scope must NAME an existing governance
        # scope-catalog entry (spec: "names the specific governance
        # scope-catalog entry ... this operation's dispatch is gated by").
        # The authoritative catalog is the SCOPE_CATALOG literal in
        # src/kiro_crew/platform/governance.py (its keys, both flat like
        # "tools"/"mcp"/"apps"/"commands" and dotted like "filesystem.read").
        # It is read from that file's source via AST at validation time
        # (see _governance_scope_catalog): NOT imported (governance.py
        # pulls a heavy kiro_crew runtime dependency chain the stdlib-only
        # CI gate has no environment for) and NOT copied into a second list
        # here (a copy would be a second authoritative source that could
        # drift). Unknown values are rejected; the accepted set is exactly
        # governance.py's own keys plus JSON null.
        #
        # Static membership is NOT runtime governance. This confirms a
        # non-null scope names a real catalog entry at manifest-authoring
        # time; it does not execute the per-layer dispatch binding or the
        # per-effect execution governance for the five scope layers. Per the
        # spec's work-stream DAG (the W01 -> W02..W14 edge: W01 produces the
        # AUTH-*/GOV-*/RUN-* contracts each stream references through its
        # manifest policy/auth_modes/retry fields), that runtime layer belongs
        # to the W01 shared control plane, not this static validator.
        catalog = _governance_scope_catalog()
        if not value.strip():
            result.add(ref, f"policy.{sub}", "must be null or a scope-catalog name, not blank")
        elif catalog is None:
            result.add(
                ref,
                f"policy.{sub}",
                "cannot validate scope membership: the authoritative SCOPE_CATALOG in "
                "src/kiro_crew/platform/governance.py could not be read — failing closed",
            )
        elif value not in catalog:
            result.add(
                ref,
                f"policy.{sub}",
                f"{value!r} is not a member of the governance SCOPE_CATALOG "
                f"(src/kiro_crew/platform/governance.py). A non-null policy scope must name an "
                f"existing catalog entry (e.g. one of {', '.join(sorted(catalog)[:4])}, ...); "
                f"use null for no additional governance constraint on this layer.",
            )


def _rung_ok(entry: dict[str, Any], rung: str) -> bool:
    """Whether ``entry`` satisfies the per-rung required-field checklist up
    to and including ``rung``, per the spec's table."""
    idx = _RUNG_INDEX.get(rung)
    if idx is None:
        return True
    # code_complete requires verification_contract + tested_sha
    if idx >= _RUNG_INDEX["code_complete"]:
        if not entry.get("verification_contract") or not entry.get("tested_sha"):
            return False
    if idx >= _RUNG_INDEX["merged"]:
        if not entry.get("merged_sha"):
            return False
    if idx >= _RUNG_INDEX["release_verified"]:
        if not entry.get("release_sha"):
            return False
    return True


def _is_commit_sha(value: Any) -> bool:
    """A commit-SHA field must hold an immutable hex object name, never a
    movable alias (a branch like ``main``, a tag, ``HEAD``). Accept a hex
    string in git's object-id length range (7-64 hex chars, covering
    abbreviated and full SHA-1/SHA-256 names); reject any non-hex character."""
    if not isinstance(value, str):
        return False
    # Validate the value verbatim — do NOT strip: a commit SHA is stored and
    # compared byte-for-byte, so surrounding whitespace makes it not a valid
    # SHA (" aaaa..." / "\taaaa\n" must be rejected, not silently accepted).
    return 7 <= len(value) <= 64 and all(c in "0123456789abcdefABCDEF" for c in value)


def _check_sha_shape(result: ValidationResult, ref: str, entry: dict[str, Any]) -> None:
    """Every non-null commit-SHA field on an entry must be an immutable hex
    object name, not a movable alias (spec: release_sha is "never a tag or
    other movable alias"; the immutable-ref binding relies on the same
    immutability for tested_sha/merged_sha). A value like "main" would
    otherwise satisfy the truthiness and equality checks and be certified as
    immutable evidence."""
    for sha_field in ("tested_sha", "merged_sha", "release_sha"):
        v = entry.get(sha_field)
        if v is not None and not _is_commit_sha(v):
            result.add(
                ref,
                sha_field,
                f"{v!r} is not an immutable commit SHA (hex object name) — a movable alias "
                "(a branch like 'main', a tag, or HEAD) must never fill a commit-SHA field",
            )


def _check_sha_null_until_rung(result: ValidationResult, ref: str, entry: dict[str, Any]) -> None:
    """Reverse of the per-rung required-field checklist: each SHA field must be
    NULL until its rung is reached (spec: tested_sha `null until code_complete`,
    merged_sha `null until merged`, release_sha `null until release_verified`).
    Without this, a `planned` entry could carry a prematurely-filled
    tested_sha/merged_sha/release_sha and be accepted — evidence fields
    populated ahead of the rung that earns them."""
    status = entry.get("status")
    last_reached = entry.get("last_reached_status")
    effective_rung = last_reached if status == "blocked" else status
    idx = _RUNG_INDEX.get(effective_rung) if isinstance(effective_rung, str) else None
    if idx is None:
        return  # unknown/invalid status is flagged elsewhere
    for sha_field, earns_at in (
        ("tested_sha", "code_complete"),
        ("merged_sha", "merged"),
        ("release_sha", "release_verified"),
    ):
        if idx < _RUNG_INDEX[earns_at] and entry.get(sha_field) is not None:
            result.add(
                ref,
                sha_field,
                f"must be null until status reaches {earns_at!r} (current effective rung "
                f"{effective_rung!r} is earlier) — an evidence SHA cannot be populated ahead "
                "of the rung that earns it",
            )


# effect values whose readback_result must be null (per the spec's per-effect
# table — "read" is the only effect that changes nothing to read back).
_READBACK_NULL_EFFECTS = frozenset({"read"})

# effect values that always have a revert/revoke/confirm path per the
# spec's own per-effect table, and whose cleanup_status can therefore never
# legally land on "not_automatable" (that value exists specifically to
# distinguish external_send/billable, where no such path may exist, from
# the ordinary case where one always does). "delete"'s own row gives only
# not_applicable (sole test artifact) or confirmed (one step of a larger
# cleanup) — never not_automatable — so it belongs in this set too.
_NO_NOT_AUTOMATABLE_EFFECTS = frozenset({"write", "admin", "share", "delete"})

# Sentinel for _check_receipt_shape's `effect` parameter meaning "the
# referencing entry's effect is UNKNOWN here" — used by the orphan-evidence
# scan (F1), which validates a receipt that no manifest entry references, so
# there is no effect to read. It must be distinguishable from `None`, which
# is a real (invalid) effect value a malformed entry could carry: treating
# an unknown effect as `None` made the readback rule fall into its
# "every effect except read" branch and falsely reject a structurally valid
# read-operation receipt (readback_result: null). With the sentinel, the
# effect-CONDITIONAL rules (readback shape, cleanup-vs-effect) are skipped
# for an orphan receipt while the effect-INDEPENDENT structural checks
# (required fields, id types, cleanup_status enum, cleanup_confirmed
# consistency) still run.
_EFFECT_UNKNOWN = object()

# ConformanceRun's own required fields per the spec's field table — every
# one of these is typed as a plain, always-required string except
# executed_at (a timestamp) and response_summary (an object, checked
# separately below). No shape check on this record existed at all: a run
# missing tested_sha/runner_version (etc.) silently passed the staleness
# guard elsewhere in this file, which treats "field absent on the run" as
# "nothing to compare" rather than "this run record is itself malformed".
_RUN_REQUIRED_STRING_FIELDS = (
    "run_id",
    "operation_id",
    "account_binding_ref",
    "auth_mode",
    "account_type",
    "surface",
    "tested_sha",
    "adapter_version",
    "input_schema_version",
    "output_schema_version",
    "runner_version",
    "request_shape_hash",
    "evidence_receipt_ref",
)


def _validate_run(result: ValidationResult, ref: str, run_ref: str, run: dict[str, Any]) -> None:
    """Structural validation of a ConformanceRun's own shape, per the
    spec's field table. The loader (``_load_jsonl``) only checks that a
    record is a JSON object with a unique, string-typed ``run_id`` — it
    never validated any of the OTHER required fields a ``ConformanceRun``
    must carry, so a run record missing ``tested_sha``/``runner_version``/
    etc. was indistinguishable from one that legitimately had those
    fields, everywhere downstream that reads this run."""
    field_prefix = f"verification_contract.run_ref[{run_ref}]"

    for field_name in _RUN_REQUIRED_STRING_FIELDS:
        value = run.get(field_name)
        if not isinstance(value, str) or not value.strip():
            result.add(
                ref,
                f"{field_prefix}.{field_name}",
                "required string field is missing, null, or blank",
            )

    executed_at = run.get("executed_at")
    if not isinstance(executed_at, str) or not executed_at.strip():
        result.add(ref, f"{field_prefix}.executed_at", "required timestamp field is missing")

    verdict = run.get("verdict")
    if not _member_of(verdict, CONFORMANCE_VERDICTS):
        result.add(
            ref,
            f"{field_prefix}.verdict",
            f"{verdict!r} is not one of {sorted(CONFORMANCE_VERDICTS)}",
        )

    response_summary = run.get("response_summary")
    if not isinstance(response_summary, dict):
        result.add(ref, f"{field_prefix}.response_summary", "required object field is missing")
    else:
        fields_present = response_summary.get("fields_present")
        if not isinstance(fields_present, list) or not all(
            isinstance(x, str) for x in fields_present
        ):
            result.add(
                ref,
                f"{field_prefix}.response_summary.fields_present",
                "must be an array of strings",
            )
        if not isinstance(response_summary.get("types_matched"), bool):
            result.add(ref, f"{field_prefix}.response_summary.types_matched", "must be a boolean")
        unexpected_fields = response_summary.get("unexpected_fields")
        if not isinstance(unexpected_fields, list) or not all(
            isinstance(x, str) for x in unexpected_fields
        ):
            result.add(
                ref,
                f"{field_prefix}.response_summary.unexpected_fields",
                "must be an array of strings",
            )


def _check_receipt_shape(
    result: ValidationResult,
    ref: str,
    receipt_ref: str,
    receipt: dict[str, Any],
    effect: Any,
) -> None:
    """Structural validation of an EvidenceReceipt's own shape, per the
    spec's field table for EvidenceReceipt and its per-effect verification
    rule. A receipt whose runtime_verified is true but whose readback_result/
    cleanup_status/cleanup_confirmed fields are absent, malformed, or
    internally inconsistent must not be accepted as certifying evidence —
    the field's own required-shape rule is enforced here, at the point the
    receipt is used to certify a rung."""
    field_prefix = f"verification_contract.receipt_ref[{receipt_ref}]"

    # Required scalar fields per the spec's EvidenceReceipt table. These were
    # not inspected here at all — a receipt missing claim/receipt_id/
    # conformance_run_ref, or carrying a non-boolean runtime_verified, could
    # certify contract_verified-or-later on the cleanup/readback checks alone.
    for str_field in ("receipt_id", "conformance_run_ref", "claim"):
        v = receipt.get(str_field)
        if not isinstance(v, str) or not v.strip():
            result.add(
                ref,
                f"{field_prefix}.{str_field}",
                "required string field is missing, null, or blank",
            )
    if not isinstance(receipt.get("runtime_verified"), bool):
        result.add(ref, f"{field_prefix}.runtime_verified", "required boolean field is missing")
    negative_test_refs = receipt.get("negative_test_refs")
    if (
        not isinstance(negative_test_refs, list)
        or len(negative_test_refs) == 0
        or not all(isinstance(x, str) and x.strip() for x in negative_test_refs)
    ):
        result.add(
            ref,
            f"{field_prefix}.negative_test_refs",
            "required non-empty array of non-empty strings (at least one negative-path test ref)",
        )

    cleanup_status = receipt.get("cleanup_status")
    if not _member_of(cleanup_status, CLEANUP_STATUSES):
        result.add(
            ref,
            f"{field_prefix}.cleanup_status",
            f"{cleanup_status!r} is not one of {sorted(CLEANUP_STATUSES)}",
        )

    cleanup_confirmed = receipt.get("cleanup_confirmed")
    if not isinstance(cleanup_confirmed, bool):
        result.add(ref, f"{field_prefix}.cleanup_confirmed", "required boolean field is missing")
    elif _member_of(cleanup_status, CLEANUP_STATUSES):
        expected = cleanup_status == "confirmed"
        if cleanup_confirmed != expected:
            result.add(
                ref,
                f"{field_prefix}.cleanup_confirmed",
                f"is {cleanup_confirmed!r} but cleanup_status={cleanup_status!r} — the spec "
                "derives cleanup_confirmed as true iff cleanup_status is 'confirmed', never "
                "independently asserted; a receipt where the two disagree is malformed",
            )

    readback_result = receipt.get("readback_result")
    # Everything below is EFFECT-CONDITIONAL (the readback null/shape rule and
    # the cleanup_status-vs-effect rule both branch on the referencing entry's
    # effect). When the effect is unknown (orphan-evidence scan, F1), skip
    # these — the structural checks above already ran, and guessing an effect
    # here would falsely reject a valid receipt (e.g. a read receipt legally
    # carries readback_result: null).
    if effect is _EFFECT_UNKNOWN:
        return
    if _member_of(effect, _READBACK_NULL_EFFECTS):
        if readback_result is not None:
            result.add(
                ref,
                f"{field_prefix}.readback_result",
                f"must be null for effect={effect!r} (nothing was changed to read back), "
                f"got {readback_result!r}",
            )
        # A read effect changes nothing, so the spec's per-effect table fixes
        # its cleanup_status at not_applicable. Check it BEFORE the early
        # return, otherwise a read receipt with cleanup_status=pending/etc.
        # certifies a rung despite an invalid cleanup state.
        if cleanup_status != "not_applicable":
            result.add(
                ref,
                f"{field_prefix}.cleanup_status",
                f"must be 'not_applicable' for effect={effect!r} (a read changes nothing to "
                f"clean up), got {cleanup_status!r}",
            )
        return

    # Every non-"read" effect's own per-effect verification row REQUIRES an
    # actual independent-read confirmation object — "null" is legal only for
    # "read" (nothing was changed to read back). A null readback_result on a
    # write-shaped effect combined with runtime_verified=true and
    # cleanup_status=pending (cleanup owed, not yet done) would otherwise
    # certify an unverified, uncleaned mutation with no structural objection.
    if readback_result is None:
        result.add(
            ref,
            f"{field_prefix}.readback_result",
            f"must not be null for effect={effect!r} — the spec's per-effect table requires "
            "an actual independent-read confirmation object for every effect except 'read'",
        )
        return

    if not isinstance(readback_result, dict):
        result.add(
            ref,
            f"{field_prefix}.readback_result",
            "must be an object {checked_at, method, matched, detail} or null",
        )
        return

    for sub in ("checked_at", "method", "matched", "detail"):
        if sub not in readback_result:
            result.add(
                ref,
                f"{field_prefix}.readback_result.{sub}",
                "required sub-field is missing",
            )
    # Per the spec's EvidenceReceipt table, readback_result is
    # {checked_at: string, method: string, matched: boolean, detail: string}.
    # checked_at/method/detail were checked for PRESENCE only — a non-string
    # (or blank) value passed, so a receipt could certify a mutation with a
    # numeric/null checked_at or an empty method and still be accepted. Type
    # them as non-empty strings here.
    for str_sub in ("checked_at", "method", "detail"):
        if str_sub in readback_result:
            sv = readback_result.get(str_sub)
            if not isinstance(sv, str) or not sv.strip():
                result.add(
                    ref,
                    f"{field_prefix}.readback_result.{str_sub}",
                    f"must be a non-empty string, got {type(sv).__name__}",
                )
    matched = readback_result.get("matched")
    if "matched" in readback_result:
        if not isinstance(matched, bool):
            result.add(ref, f"{field_prefix}.readback_result.matched", "must be a boolean")
        elif matched is not True:
            # A non-"read" effect's receipt exists to certify the mutation
            # actually took place as claimed. matched=false/absent means the
            # independent readback did NOT confirm that — such a receipt
            # cannot be the basis for a passing verification_contract.
            result.add(
                ref,
                f"{field_prefix}.readback_result.matched",
                f"is {matched!r} for effect={effect!r} — a receipt whose own independent "
                "readback did not confirm the mutation cannot certify this verification_contract",
            )

    # write/admin/share always have a revert/revoke path per the spec's own
    # table ("the write/admin cases do not share [not_automatable] with
    # external_send/billable"); not_automatable is illegal cleanup ground
    # for them. delete may legitimately be not_applicable (if the deleted
    # resource was itself the sole test artifact) or confirmed.
    # external_send/billable may legitimately be not_automatable. pending is
    # legal everywhere (cleanup owed, not yet done) — it is not, on its own,
    # a certifying value, but this receipt-shape check does not gate rung
    # promotion; it only rejects a cleanup_status the effect can never
    # legally reach.
    if _member_of(effect, _NO_NOT_AUTOMATABLE_EFFECTS) and cleanup_status == "not_automatable":
        result.add(
            ref,
            f"{field_prefix}.cleanup_status",
            f"'not_automatable' is not a legal cleanup_status for effect={effect!r} — the "
            "spec's per-effect table gives this effect an always-available revert/revoke path",
        )


def _check_immutable_ref_binding(
    result: ValidationResult, ref: str, entry: dict[str, Any], run: dict[str, Any]
) -> None:
    """Immutable-ref-binding staleness: the entry's five bound version fields
    (spec lines 323-328) must equal the referenced ConformanceRun's recorded
    values — tested_sha, runner_version, adapter.version,
    input_schema.schema_version, output_schema.schema_version. Shared by the
    entry-level verification_contract check AND the per-cell matrix check so a
    cell reference is not validated more shallowly than the entry-level one."""
    for entry_field, run_field in (
        ("tested_sha", "tested_sha"),
        ("runner_version", "runner_version"),
    ):
        entry_value = entry.get(entry_field)
        run_value = run.get(run_field)
        if entry_value is not None and run_value is not None and entry_value != run_value:
            result.add(
                ref,
                entry_field,
                f"entry's {entry_field}={entry_value!r} no longer matches the referenced "
                f"ConformanceRun's {run_field}={run_value!r} — verification_contract is STALE, "
                f"repoint it to a newer run per the spec's immutable-ref-binding rule",
            )
    adapter = entry.get("adapter") or {}
    if isinstance(adapter, dict):
        entry_adapter_version = adapter.get("version")
        run_adapter_version = run.get("adapter_version")
        if (
            entry_adapter_version is not None
            and run_adapter_version is not None
            and entry_adapter_version != run_adapter_version
        ):
            result.add(
                ref,
                "adapter.version",
                f"entry's adapter.version={entry_adapter_version!r} no longer matches the "
                f"referenced ConformanceRun's adapter_version={run_adapter_version!r} — stale",
            )
    for schema_field, run_field in (
        ("input_schema", "input_schema_version"),
        ("output_schema", "output_schema_version"),
    ):
        schema_obj = entry.get(schema_field) or {}
        if not isinstance(schema_obj, dict):
            continue
        entry_schema_version = schema_obj.get("schema_version")
        run_schema_version = run.get(run_field)
        if (
            entry_schema_version is not None
            and run_schema_version is not None
            and entry_schema_version != run_schema_version
        ):
            result.add(
                ref,
                f"{schema_field}.schema_version",
                f"entry's {schema_field}.schema_version={entry_schema_version!r} no longer "
                f"matches the referenced ConformanceRun's {run_field}={run_schema_version!r} "
                "— verification_contract is STALE, repoint it to a newer run per the spec's "
                "immutable-ref-binding rule",
            )


def _check_verification_contract(result: ValidationResult, ref: str, entry: dict[str, Any]) -> None:
    status = entry.get("status")
    last_reached = entry.get("last_reached_status")
    vc = entry.get("verification_contract")
    service_id = entry.get("service_id")

    effective_rung = last_reached if status == "blocked" else status

    if effective_rung not in STATUS_LADDER:
        return  # already flagged elsewhere as an invalid status/last_reached_status

    if _RUNG_INDEX[effective_rung] < _RUNG_INDEX["code_complete"]:
        # planned or implementing: no ConformanceRun exists yet, verification_contract stays null.
        if vc is not None:
            result.add(
                ref,
                "verification_contract",
                f"must be null while status/last_reached_status is {effective_rung!r} "
                "(only required once the rung reaches code_complete)",
            )
        return

    if vc is None:
        result.add(
            ref,
            "verification_contract",
            f"required and non-null once status reaches {effective_rung!r} (or later)",
        )
        return

    if not isinstance(vc, dict) or "run_ref" not in vc or "receipt_ref" not in vc:
        result.add(ref, "verification_contract", "must be {run_ref, receipt_ref}")
        return

    if not isinstance(service_id, str) or service_id not in SERVICE_IDS:
        return  # already flagged by service_id check; cannot resolve cross-refs

    run_ref = vc["run_ref"]
    receipt_ref = vc["receipt_ref"]

    try:
        runs = _runs_for_service(service_id)
        receipts = _receipts_for_service(service_id)
    except ValueError as exc:
        result.add(ref, "verification_contract", f"malformed evidence log: {exc}")
        return

    run = _find_by_id(runs, "run_id", run_ref)
    if run is None:
        result.add(
            ref,
            "verification_contract.run_ref",
            f"{run_ref!r} does not resolve to any ConformanceRun for service {service_id!r}",
        )
        return

    receipt = _find_by_id(receipts, "receipt_id", receipt_ref)
    if receipt is None:
        result.add(
            ref,
            "verification_contract.receipt_ref",
            f"{receipt_ref!r} does not resolve to any EvidenceReceipt for service {service_id!r}",
        )
        return

    _validate_run(result, ref, run_ref, run)
    _check_receipt_shape(result, ref, receipt_ref, receipt, entry.get("effect"))

    # Three-way equality, per the spec.
    if run.get("operation_id") != entry.get("operation_id"):
        result.add(
            ref,
            "verification_contract.run_ref",
            f"referenced ConformanceRun.operation_id {run.get('operation_id')!r} "
            f"!= this entry's own operation_id {entry.get('operation_id')!r}",
        )
    if receipt.get("conformance_run_ref") != run_ref:
        result.add(
            ref,
            "verification_contract.receipt_ref",
            f"referenced EvidenceReceipt.conformance_run_ref {receipt.get('conformance_run_ref')!r} "
            f"!= verification_contract.run_ref {run_ref!r}",
        )
    if run.get("evidence_receipt_ref") != receipt_ref:
        result.add(
            ref,
            "verification_contract.receipt_ref",
            f"referenced ConformanceRun.evidence_receipt_ref {run.get('evidence_receipt_ref')!r} "
            f"!= verification_contract.receipt_ref {receipt_ref!r}",
        )

    # A status transition past code_complete is valid only against a
    # passing run — but the spec's own per-rung table only REQUIRES a
    # verdict/runtime_verified check starting at contract_verified.
    # code_complete itself explicitly means "has not yet run a live
    # ConformanceRun" (see the status-value table above), so enforcing
    # verdict==pass at code_complete would reject the spec's own legitimate
    # unit-tested-but-not-live-tested rung.
    if (
        effective_rung in STATUS_LADDER
        and _RUNG_INDEX[effective_rung] >= _RUNG_INDEX["contract_verified"]
    ):
        if run.get("verdict") != "pass":
            result.add(
                ref,
                "verification_contract.run_ref",
                f"entry claims {effective_rung!r} but the referenced ConformanceRun's "
                f"verdict is {run.get('verdict')!r}, not 'pass'",
            )
        receipt_runtime_verified = receipt.get("runtime_verified")
        if receipt_runtime_verified is not True:
            result.add(
                ref,
                "verification_contract.receipt_ref",
                f"entry claims {effective_rung!r} but the referenced EvidenceReceipt has "
                f"runtime_verified={receipt_runtime_verified!r}, not true",
            )

    # release_verified: the run's tested_sha must equal this entry's release_sha.
    if effective_rung == "release_verified":
        release_sha = entry.get("release_sha")
        if release_sha and run.get("tested_sha") != release_sha:
            result.add(
                ref,
                "release_sha",
                f"entry's release_sha {release_sha!r} != the certifying ConformanceRun's "
                f"tested_sha {run.get('tested_sha')!r} — the run must target the release SHA itself",
            )

    # Immutable ref binding: the entry's top-level version fields must match
    # the run's own recorded fields once code_complete or later (staleness
    # check). Delegated to the shared helper so the per-cell matrix check can
    # apply the SAME five-field staleness comparison (spec lines 323-328),
    # rather than a shallower per-cell check.
    _check_immutable_ref_binding(result, ref, entry, run)

    # The spec's own status table requires contract_verified to be certified
    # by "a ConformanceRun ... against at least one real, authorized
    # (auth_mode, account_type, surface) combination the entry declares" —
    # but nothing above checked that the referenced run's own
    # auth_mode/account_type/surface actually matches any cell this entry's
    # own evidence matrix declares applicable. An entry could reach
    # contract_verified with every declared cell still
    # sitting at implementing and a top-level run for an undeclared
    # coordinate, because the live_verified-only completeness check
    # (_check_evidence_matrix) never runs at this rung. Checked here,
    # entry-level, so it applies at contract_verified and later regardless
    # of whether live_verified's stronger per-cell check ever runs.
    if _RUNG_INDEX[effective_rung] >= _RUNG_INDEX["contract_verified"]:
        run_coords = (run.get("auth_mode"), run.get("account_type"), run.get("surface"))
        matrix = entry.get("evidence_by_mode_surface_and_auth")
        entry_auth_modes = entry.get("auth_modes") or []
        entry_account_types = entry.get("account_types") or []
        entry_surfaces = entry.get("surfaces") or []
        declared_cross_product = set()
        if (
            isinstance(entry_auth_modes, list)
            and isinstance(entry_account_types, list)
            and isinstance(entry_surfaces, list)
        ):
            declared_cross_product = {
                (a, t, s)
                for a in entry_auth_modes
                for t in entry_account_types
                for s in entry_surfaces
                if isinstance(a, str) and isinstance(t, str) and isinstance(s, str)
            }
        declared_applicable_coords = set()
        if isinstance(matrix, list):
            for row in matrix:
                if not isinstance(row, dict):
                    continue
                if row.get("applicable") is True:
                    coord = (row.get("auth_mode"), row.get("account_type"), row.get("surface"))
                    if all(isinstance(c, str) for c in coord) and coord in declared_cross_product:
                        # A row is only a legitimate applicable cell if its own
                        # coordinate is actually a member of THIS entry's own
                        # declared auth_modes x account_types x surfaces
                        # cross-product — a row claiming applicable: true for
                        # a coordinate outside that cross-product (a rogue
                        # value never declared on any of the three axes) must
                        # never count as a real, certifiable cell, regardless
                        # of what the row itself asserts about itself.
                        declared_applicable_coords.add(coord)
        if all(isinstance(c, str) for c in run_coords) and (
            run_coords not in declared_applicable_coords
        ):
            result.add(
                ref,
                "verification_contract.run_ref",
                f"entry claims {effective_rung!r} but the referenced ConformanceRun's own "
                f"(auth_mode, account_type, surface)={run_coords!r} does not match any "
                "applicable=true row in this entry's own evidence_by_mode_surface_and_auth — "
                "the spec requires contract_verified to be certified against a combination "
                "the entry itself declares, not an arbitrary or undeclared one",
            )


def _check_evidence_matrix(result: ValidationResult, ref: str, entry: dict[str, Any]) -> None:
    status = entry.get("status")
    matrix = entry.get("evidence_by_mode_surface_and_auth")
    if matrix is None:
        result.add(
            ref,
            "evidence_by_mode_surface_and_auth",
            "required field is missing (use [] at status=planned)",
        )
        return
    if not isinstance(matrix, list):
        result.add(ref, "evidence_by_mode_surface_and_auth", "must be an array")
        return

    effective_status = entry.get("last_reached_status") if status == "blocked" else status

    # At status=planned the matrix may be EMPTY or partially populated — the
    # TOTALITY requirement (one row per declared coordinate) is waived. But any
    # rows that ARE present must still obey row-level structure: their
    # coordinates must be declared, an excluded row must not cite evidence, etc.
    # So planned only waives the missing-rows (totality) check below; it does
    # NOT skip the per-row structural checks. (An empty planned matrix simply
    # has no rows to check and passes.)
    is_planned = effective_status == "planned"

    auth_modes = entry.get("auth_modes") or []
    account_types = entry.get("account_types") or []
    surfaces = entry.get("surfaces") or []
    if not (
        isinstance(auth_modes, list)
        and isinstance(account_types, list)
        and isinstance(surfaces, list)
    ):
        return  # already flagged elsewhere

    # An element of any of the three axes can itself be an unhashable JSON
    # type (e.g. auth_modes: [[]]) — building the (a, t, s) tuple below and
    # putting it in a set literal then raises TypeError, crashing the whole
    # gate on one malformed entry. Every element of every axis must be a
    # string before any axis is used to build the matrix; a non-string
    # element is reported as its own Finding, not a crash.
    axes_ok = True
    for axis_name, axis in (
        ("auth_modes", auth_modes),
        ("account_types", account_types),
        ("surfaces", surfaces),
    ):
        for i, element in enumerate(axis):
            if not isinstance(element, str):
                result.add(
                    ref,
                    f"{axis_name}[{i}]",
                    f"{element!r} must be a string to be used as a matrix coordinate",
                )
                axes_ok = False
    if not axes_ok:
        return

    expected_triples = {(a, t, s) for a in auth_modes for t in account_types for s in surfaces}
    seen_triples: set[tuple[Any, Any, Any]] = set()
    duplicate_triples: set[tuple[Any, Any, Any]] = set()

    service_id = entry.get("service_id")
    try:
        runs = (
            _runs_for_service(service_id)
            if isinstance(service_id, str) and service_id in SERVICE_IDS
            else []
        )
        receipts = (
            _receipts_for_service(service_id)
            if isinstance(service_id, str) and service_id in SERVICE_IDS
            else []
        )
    except ValueError as exc:
        result.add(ref, "evidence_by_mode_surface_and_auth", f"malformed evidence log: {exc}")
        return

    all_applicable_at_contract_verified_or_later = True

    for i, row in enumerate(matrix):
        row_ref = f"{ref}.evidence_by_mode_surface_and_auth[{i}]"
        if not isinstance(row, dict):
            result.add(row_ref, "<row>", "must be an object")
            continue
        row_coords = (row.get("auth_mode"), row.get("account_type"), row.get("surface"))
        if not all(isinstance(c, str) for c in row_coords):
            result.add(
                row_ref,
                "auth_mode/account_type/surface",
                f"{row_coords!r} — each of these three fields must be a string to be "
                "used as a matrix coordinate",
            )
            continue
        triple = row_coords
        if triple in seen_triples:
            duplicate_triples.add(triple)
        seen_triples.add(triple)

        # Row lifecycle validation runs for EVERY row (including excluded
        # ones) BEFORE branching on applicable — otherwise an excluded row, or
        # an applicable row whose status contradicts last_reached_status, skips
        # this via the `continue` below and corrupted lifecycle state passes.
        row_status_any = row.get("status")
        row_last_reached_any = row.get("last_reached_status")
        if not _member_of(row_status_any, STATUS_VALUES):
            result.add(
                row_ref,
                "status",
                f"{row_status_any!r} is not one of {sorted(STATUS_VALUES)}",
            )
        if not _member_of(row_last_reached_any, LAST_REACHED_VALUES):
            result.add(
                row_ref,
                "last_reached_status",
                f"{row_last_reached_any!r} is not one of {sorted(LAST_REACHED_VALUES)}",
            )
        elif row_status_any != "blocked" and row_last_reached_any != row_status_any:
            # When a row is not blocked, last_reached_status must equal status
            # (same rule the entry level applies) — a row claiming a different
            # last-reached rung than its live status is malformed.
            result.add(
                row_ref,
                "last_reached_status",
                f"must equal status ({row_status_any!r}) when the row is not blocked, "
                f"got {row_last_reached_any!r}",
            )

        applicable = row.get("applicable")
        if applicable is False:
            if not row.get("exclusion_reason"):
                result.add(
                    row_ref, "exclusion_reason", "required and non-null when applicable=false"
                )
            # An excluded cell has no evidence, so it must not also claim a
            # verification_contract_ref — a row cannot be both "not applicable"
            # and pointing at a conformance run.
            if row.get("verification_contract_ref") is not None:
                result.add(
                    row_ref,
                    "verification_contract_ref",
                    "must be null when applicable=false — an excluded cell has no evidence to cite",
                )
            continue
        if applicable is not True:
            result.add(row_ref, "applicable", "must be true or false")
            continue

        # row status/last_reached enum + consistency already validated above
        # for every row (universal check); here just derive the rung.
        row_status = row.get("status")
        row_rung = row.get("last_reached_status") if row_status == "blocked" else row_status
        vc_ref = row.get("verification_contract_ref")

        if row_rung == "planned" or row_rung == "implementing":
            if vc_ref is not None:
                result.add(
                    row_ref,
                    "verification_contract_ref",
                    "must be null while this cell's status is planned or implementing",
                )
        elif row_rung in STATUS_LADDER and _RUNG_INDEX[row_rung] >= _RUNG_INDEX["code_complete"]:
            if vc_ref is None:
                result.add(
                    row_ref,
                    "verification_contract_ref",
                    f"required once this cell's status reaches {row_rung!r}",
                )
            else:
                run = _find_by_id(runs, "run_id", vc_ref)
                if run is None:
                    result.add(
                        row_ref,
                        "verification_contract_ref",
                        f"{vc_ref!r} does not resolve to any ConformanceRun for service {service_id!r}",
                    )
                else:
                    # F2: a cell reference must be validated to the SAME
                    # depth as the entry-level verification_contract, not
                    # merely coordinate-matched. Previously this per-cell
                    # path checked only coordinates, verdict, and the
                    # receipt's runtime_verified, so a cell whose run was
                    # missing required fields (or whose receipt had a wrong
                    # back-pointer) could still promote. Reuse the full
                    # run-shape validation here.
                    _validate_run(result, row_ref, vc_ref, run)
                    # Apply the SAME five-field immutable-ref staleness check
                    # the entry level applies, so a cell run whose bound fields
                    # (tested_sha, runner_version, adapter.version, the two
                    # schema versions) drift from the entry is caught here too,
                    # not only at the entry level. Skip it when this cell cites
                    # the SAME run as the entry-level verification_contract —
                    # that run's staleness is already reported at the entry ref,
                    # and re-checking here would duplicate the finding.
                    entry_vc = entry.get("verification_contract")
                    entry_vc_run_ref = (
                        entry_vc.get("run_ref") if isinstance(entry_vc, dict) else None
                    )
                    if vc_ref != entry_vc_run_ref:
                        _check_immutable_ref_binding(result, row_ref, entry, run)
                    for coord_field, run_field in (
                        ("operation_id", "operation_id"),
                        ("auth_mode", "auth_mode"),
                        ("account_type", "account_type"),
                        ("surface", "surface"),
                    ):
                        expected = (
                            entry.get("operation_id")
                            if coord_field == "operation_id"
                            else row.get(coord_field)
                        )
                        if run.get(run_field) != expected:
                            result.add(
                                row_ref,
                                "verification_contract_ref",
                                f"referenced ConformanceRun.{run_field}={run.get(run_field)!r} "
                                f"!= this cell's own {coord_field}={expected!r} — a run for a "
                                "different coordinate never promotes this cell",
                            )
                    # Same spec-grounded rung as the entry-level check above:
                    # code_complete itself only requires vc_ref to be
                    # non-null and resolvable, never a passing verdict — the
                    # spec's own status table says code_complete "has not
                    # yet run a live ConformanceRun". The verdict check is
                    # gated separately, at contract_verified or later.
                    if _RUNG_INDEX[row_rung] >= _RUNG_INDEX["contract_verified"]:
                        if run.get("verdict") != "pass":
                            result.add(
                                row_ref,
                                "verification_contract_ref",
                                f"referenced ConformanceRun.verdict={run.get('verdict')!r}, not 'pass'",
                            )
                        # The entry-level check (_check_verification_contract)
                        # additionally resolves the run's own receipt and
                        # requires runtime_verified: true before certifying
                        # contract_verified-or-later. This per-cell check
                        # must perform the equivalent receipt/
                        # runtime_verified resolution too, so a passing run whose
                        # receipt was never actually runtime-verified could
                        # still promote this specific cell.
                        cell_receipt_ref = run.get("evidence_receipt_ref")
                        cell_receipt = (
                            _find_by_id(receipts, "receipt_id", cell_receipt_ref)
                            if cell_receipt_ref is not None
                            else None
                        )
                        if cell_receipt is None:
                            result.add(
                                row_ref,
                                "verification_contract_ref",
                                f"referenced ConformanceRun's evidence_receipt_ref "
                                f"{cell_receipt_ref!r} does not resolve to any EvidenceReceipt "
                                f"for service {service_id!r}",
                            )
                        else:
                            # F2: validate the receipt's full shape and its
                            # immutable three-way binding, exactly as the
                            # entry-level check does — not just
                            # runtime_verified. A receipt with a wrong
                            # conformance_run_ref back-pointer, or missing
                            # required fields, must not silently promote a
                            # cell.
                            _check_receipt_shape(
                                result,
                                row_ref,
                                str(cell_receipt_ref),
                                cell_receipt,
                                entry.get("effect"),
                            )
                            if cell_receipt.get("conformance_run_ref") != vc_ref:
                                result.add(
                                    row_ref,
                                    "verification_contract_ref",
                                    f"referenced EvidenceReceipt.conformance_run_ref "
                                    f"{cell_receipt.get('conformance_run_ref')!r} != this cell's "
                                    f"ConformanceRun run_id {vc_ref!r} — the receipt certifies a "
                                    "different run",
                                )
                            if cell_receipt.get("runtime_verified") is not True:
                                result.add(
                                    row_ref,
                                    "verification_contract_ref",
                                    f"referenced ConformanceRun's EvidenceReceipt has "
                                    f"runtime_verified={cell_receipt.get('runtime_verified')!r}, "
                                    "not true — this cell cannot promote on an unverified receipt",
                                )

        if (
            row_rung not in STATUS_LADDER
            or _RUNG_INDEX[row_rung] < _RUNG_INDEX["contract_verified"]
        ):
            all_applicable_at_contract_verified_or_later = False

    if duplicate_triples:
        result.add(
            ref,
            "evidence_by_mode_surface_and_auth",
            f"duplicate (auth_mode, account_type, surface) rows: {sorted(duplicate_triples)}",
        )

    missing_triples = expected_triples - seen_triples
    if missing_triples and not is_planned:
        result.add(
            ref,
            "evidence_by_mode_surface_and_auth",
            f"matrix is not TOTAL: missing rows for {sorted(missing_triples)} "
            f"(status={status!r} has moved past planned, so every "
            "auth_modes x account_types x surfaces combination requires exactly one row)",
        )

    # Extra rows for coordinates the entry does not declare must be rejected:
    # a (auth_mode, account_type, surface) not in the auth_modes x
    # account_types x surfaces product is an undeclared coordinate, and a
    # totality check that only looked at expected-minus-seen would silently
    # accept it (inflating evidence with rows for axes the operation never
    # claims to support).
    extra_triples = seen_triples - expected_triples
    if extra_triples:
        result.add(
            ref,
            "evidence_by_mode_surface_and_auth",
            f"matrix has rows for UNDECLARED (auth_mode, account_type, surface) "
            f"coordinates: {sorted(extra_triples)} — every row's coordinate must be in the "
            "entry's own auth_modes x account_types x surfaces product",
        )

    # The per-rung checklist is cumulative (merged's own row states "the
    # above, plus merged_sha" — "the above" being every earlier rung's own
    # requirements) — so a merged or release_verified entry must ALSO
    # satisfy live_verified's own completeness requirement, not just the
    # rungs's own newly-added field. Gating this solely on
    # effective_status == "live_verified" let a merged/release_verified
    # entry skip the completeness check entirely.
    if (
        effective_status in STATUS_LADDER
        and _RUNG_INDEX[effective_status] >= _RUNG_INDEX["live_verified"]
        and not all_applicable_at_contract_verified_or_later
    ):
        result.add(
            ref,
            "status",
            f"claims {effective_status!r} (which requires live_verified's own "
            "completeness per the cumulative per-rung checklist) but at least one "
            "applicable=true cell has not itself reached contract_verified or later",
        )


def _check_rung_checklist(result: ValidationResult, ref: str, entry: dict[str, Any]) -> None:
    status = entry.get("status")
    last_reached = entry.get("last_reached_status")

    if status is not None and not _member_of(status, STATUS_VALUES):
        result.add(ref, "status", f"{status!r} is not one of {sorted(STATUS_VALUES)}")
    if last_reached is not None and not _member_of(last_reached, LAST_REACHED_VALUES):
        result.add(
            ref,
            "last_reached_status",
            f"{last_reached!r} is not one of {sorted(LAST_REACHED_VALUES)} (blocked is not a legal last_reached_status value)",
        )

    if status != "blocked" and status != last_reached:
        result.add(
            ref,
            "last_reached_status",
            f"must equal status ({status!r}) when the entry is not currently blocked, got {last_reached!r}",
        )

    effective_rung = last_reached if status == "blocked" else status
    if effective_rung in STATUS_LADDER and not _rung_ok(entry, effective_rung):
        result.add(
            ref,
            "status",
            f"claims {effective_rung!r} but is missing a required field for that rung "
            "(see the spec's per-rung checklist: verification_contract+tested_sha from "
            "code_complete, merged_sha from merged, release_sha from release_verified)",
        )

    if status == "blocked":
        blocker = entry.get("blocker")
        if not isinstance(blocker, dict):
            result.add(ref, "blocker", "required object when status=blocked")
        else:
            for sub in ("reason", "owner", "unblock_action"):
                if not blocker.get(sub):
                    result.add(
                        ref, f"blocker.{sub}", "required non-empty sub-field when status=blocked"
                    )
    elif entry.get("blocker") is not None:
        result.add(ref, "blocker", "must be null when status is not blocked")


# ---------------------------------------------------------------------------
# Top-level entry validation
# ---------------------------------------------------------------------------

_REQUIRED_ALWAYS = (
    "operation_id",
    "operation_kind",
    "provider",
    "service_id",
    "required",
    "category",
    "source_status",
    "source",
    "observed_at",
    "effect",
    "input_schema",
    "output_schema",
    "tool_names",
    "auth_modes",
    "scopes",
    "account_types",
    "surfaces",
    "policy",
    "adapter",
    "runner_version",
    "evidence_by_mode_surface_and_auth",
    "status",
    "last_reached_status",
)

# Required, but legitimately JSON null at early rungs — nullness here is
# governed by the rung-conditional checks elsewhere (e.g.
# _check_verification_contract), not by this top-level presence pass. A
# blanket "present but null is an error" rule would wrongly reject a
# perfectly valid `planned` entry, which the spec requires to leave these
# unset.
_REQUIRED_BUT_RUNG_CONDITIONAL_NULL = (
    "verification_contract",
    "tested_sha",
    "merged_sha",
    "release_sha",
)


def validate_entry(entry: dict[str, Any], ref: str) -> ValidationResult:
    result = ValidationResult(ok=True)

    for field_name in _REQUIRED_ALWAYS:
        _require_non_null(result, ref, entry, field_name)
    for field_name in _REQUIRED_BUT_RUNG_CONDITIONAL_NULL:
        _require(result, ref, entry, field_name)

    if not isinstance(entry.get("required"), bool):
        result.add(ref, "required", "must be a boolean")

    # Scalar fields the spec types as `string`. A validator that never
    # checked these accepted a non-string scalar (a number, a bool) for a
    # documented string field silently. `runner_version` is included: the
    # spec types it `string, yes` (line 182), and a numeric value there
    # would pass the presence-only `_require_non_null` while being the
    # wrong type. `pagination`, when present, is also a `string` per the
    # spec (line 178) — its required-WHEN condition is enforced separately
    # in `_check_pagination`; here we only reject a non-string value so a
    # numeric/object pagination contract cannot slip through.
    for field_name in ("operation_id", "provider", "observed_at", "runner_version"):
        value = entry.get(field_name)
        if value is not None and not isinstance(value, str):
            result.add(ref, field_name, f"must be a string, got {type(value).__name__}")

    pagination = entry.get("pagination")
    if pagination is not None and not isinstance(pagination, str):
        result.add(ref, "pagination", f"must be a string, got {type(pagination).__name__}")

    # Array fields the spec types as `array` of strings. Every element must
    # itself be a string — an array field passing isinstance(..., list)
    # while holding a non-string element (a number, an object, a nested
    # list) must not be accepted as-is.
    for field_name in ("tool_names", "auth_modes", "scopes", "account_types", "surfaces"):
        value = entry.get(field_name)
        if value is None:
            continue
        if not isinstance(value, list):
            result.add(ref, field_name, f"must be an array, got {type(value).__name__}")
            continue
        for i, element in enumerate(value):
            if not isinstance(element, str):
                result.add(
                    ref,
                    f"{field_name}[{i}]",
                    f"must be a string, got {type(element).__name__}",
                )

    # code_refs: array of strings, but never required (an empty array is
    # always legal) — only element type is checked when present.
    code_refs = entry.get("code_refs")
    if code_refs is not None:
        if not isinstance(code_refs, list):
            result.add(ref, "code_refs", f"must be an array, got {type(code_refs).__name__}")
        else:
            for i, element in enumerate(code_refs):
                if not isinstance(element, str):
                    result.add(
                        ref,
                        f"code_refs[{i}]",
                        f"must be a string, got {type(element).__name__}",
                    )

    _check_enum(result, ref, entry, "service_id", SERVICE_IDS)
    _check_enum(result, ref, entry, "category", CATEGORIES)
    _check_enum(result, ref, entry, "source_status", SOURCE_STATUSES)
    _check_enum(result, ref, entry, "effect", EFFECTS)
    _check_enum(result, ref, entry, "operation_kind", OPERATION_KINDS)

    for schema_field in ("input_schema", "output_schema"):
        schema = entry.get(schema_field)
        if not isinstance(schema, dict):
            result.add(ref, schema_field, "required object field is missing or not an object")
        else:
            for sub in ("schema_ref", "schema_version"):
                if sub not in schema or schema.get(sub) is None:
                    result.add(
                        ref, f"{schema_field}.{sub}", "required sub-field is missing or null"
                    )
                elif sub == "schema_ref" and (
                    not isinstance(schema[sub], str) or not schema[sub].strip()
                ):
                    result.add(ref, f"{schema_field}.{sub}", "required sub-field is blank")
                elif sub == "schema_version" and (
                    not isinstance(schema[sub], str) or not schema[sub].strip()
                ):
                    # schema_version is `string` per the spec (lines 170-171);
                    # a numeric version must not pass a presence-only check
                    # while being the wrong type.
                    result.add(
                        ref,
                        f"{schema_field}.{sub}",
                        f"must be a non-empty string, got {type(schema[sub]).__name__}",
                    )
            _check_schema_ref(result, ref, schema_field, schema)

    tool_names = entry.get("tool_names")
    if isinstance(tool_names, list) and len(tool_names) == 0:
        result.add(ref, "tool_names", "an entry naming no tool is not yet implementable")

    _check_source(result, ref, entry)
    _check_adapter(result, ref, entry)
    _check_retry(result, ref, entry)
    _check_pagination(result, ref, entry)
    _check_policy(result, ref, entry)
    _check_rung_checklist(result, ref, entry)
    _check_verification_contract(result, ref, entry)
    _check_evidence_matrix(result, ref, entry)
    _check_sha_null_until_rung(result, ref, entry)
    _check_sha_shape(result, ref, entry)

    # operation_id/service_id must agree with the file's own on-disk location.
    op_id = entry.get("operation_id")
    svc_id = entry.get("service_id")
    if isinstance(op_id, str) and isinstance(svc_id, str) and svc_id in SERVICE_IDS:
        # The canonical on-disk path for an entry is deterministic in its
        # own operation_id/service_id: ENTRIES_ROOT/<service_id>/<op>.json.
        # Compare the entry file's own repository-relative path for EXACT
        # equality, not a suffix match: a suffix test (`ref.endswith(...)`)
        # would accept a rogue file at e.g.
        # `entries/rogue/entries/github/<op>.json` alongside the canonical
        # file, letting two records claim one operation identity. `ref` is
        # REPO_ROOT-relative (run_scan passes os.path.relpath(path,
        # REPO_ROOT)), so derive the expected path from the same base via
        # ENTRIES_ROOT; normalize both sides so separator/`.`-segment
        # differences do not defeat the equality.
        expected_abs = os.path.join(ENTRIES_ROOT, svc_id, f"{op_id}.json")
        expected_rel = os.path.normpath(os.path.relpath(expected_abs, REPO_ROOT))
        actual_rel = os.path.normpath(ref)
        if actual_rel != expected_rel:
            result.add(
                ref,
                "operation_id/service_id",
                f"entry's own operation_id/service_id resolve to {expected_rel!r}, "
                f"which does not match this file's actual path {actual_rel!r} — a validator "
                "resolves an entry's path deterministically from these two fields",
            )

    return result


# ---------------------------------------------------------------------------
# Repo-wide scan
# ---------------------------------------------------------------------------


def _iter_entry_files() -> list[str]:
    if not os.path.isdir(ENTRIES_ROOT):
        return []
    paths: list[str] = []
    for root, _dirs, files in os.walk(ENTRIES_ROOT):
        for name in files:
            if name.endswith(".json"):
                paths.append(os.path.join(root, name))
    return sorted(paths)


def run_scan(entry_paths: list[str] | None = None) -> ValidationResult:
    combined = ValidationResult(ok=True)
    paths = entry_paths if entry_paths is not None else _iter_entry_files()
    for path in paths:
        rel = os.path.relpath(path, REPO_ROOT)
        try:
            with open(path, encoding="utf-8") as fh:
                entry = json.load(fh)
        except json.JSONDecodeError as exc:
            combined.add(rel, "<file>", f"invalid JSON: {exc}")
            continue
        except OSError as exc:
            # A path passed via --entry that does not exist (or is otherwise
            # unreadable) must fail closed with a finding, not raise an
            # uncaught OSError/FileNotFoundError out of the gate.
            combined.add(rel, "<file>", f"could not be read ({exc.__class__.__name__})")
            continue
        if not isinstance(entry, dict):
            combined.add(rel, "<file>", "top-level JSON value must be an object")
            continue
        sub_result = validate_entry(entry, rel)
        combined.ok = combined.ok and sub_result.ok
        combined.findings.extend(sub_result.findings)

    # F1: every committed ConformanceRun/EvidenceReceipt record must be
    # structurally validated INDEPENDENTLY of whether any manifest entry
    # references it. The entry-driven pass above only reaches a run/receipt
    # through an entry's verification_contract or a matrix cell, so an
    # orphan or malformed record in runs/*.jsonl or receipts/*.jsonl (a
    # duplicate id, a non-object line, a run missing required fields, a
    # receipt missing claim/negative_test_refs) would ship uninspected and
    # the gate would report zero findings. Enumerate and check every record
    # here. When the record set is scoped (entry_paths given for a
    # single-file check), skip the whole-tree evidence scan — a targeted
    # single-entry validation must not fail on unrelated evidence files.
    if entry_paths is None:
        _scan_orphan_evidence(combined)
        # Whole-tree invariant: the authoritative governance SCOPE_CATALOG must
        # be statically readable, or membership validation is silently
        # toothless whenever no entry references a non-null scope. Checked here
        # (not per-entry) so an unreadable catalog fails the gate regardless.
        _check_scope_catalog_readable(combined)
        # Cross-check the mirrored evidence-catalog's reconciliation counts,
        # giving that extract a real machine consumer (FP review) and holding
        # the required-count denominators machine-checked (Design review).
        _check_catalog_evidence_counts(combined)

    return combined


def _iter_jsonl_files(root: str) -> list[str]:
    if not os.path.isdir(root):
        return []
    out: list[str] = []
    for name in os.listdir(root):
        if name.endswith(".jsonl"):
            out.append(os.path.join(root, name))
    return sorted(out)


def _scan_orphan_evidence(combined: "ValidationResult") -> None:
    """Structurally validate every runs/*.jsonl and receipts/*.jsonl record
    on its own, independent of manifest references (F1), AND resolve their
    run<->receipt back-pointers so an orphan run naming a nonexistent receipt
    (or vice versa) fails the gate rather than shipping a dangling evidence
    graph."""
    for run_path in _iter_jsonl_files(RUNS_ROOT):
        rel = os.path.relpath(run_path, REPO_ROOT)
        service_id = os.path.splitext(os.path.basename(run_path))[0]
        try:
            runs = _load_jsonl(run_path, id_field="run_id")
        except ValueError as exc:
            combined.add(rel, "<file>", f"malformed ConformanceRun log: {exc}")
            continue
        # Receipts for the SAME service, to resolve each run's own
        # evidence_receipt_ref. A missing receipts file yields [].
        try:
            svc_receipts = _receipts_for_service(service_id) if service_id in SERVICE_IDS else []
        except ValueError:
            svc_receipts = []
        for rec in runs:
            run_id = rec.get("run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                combined.add(rel, "run_id", "required string field is missing, null, or blank")
                continue
            _validate_run(combined, rel, run_id, rec)
            # Back-pointer resolution: a run's evidence_receipt_ref, when
            # present, must resolve to a real receipt for this service, and
            # that receipt's conformance_run_ref must point back at this run.
            receipt_ref = rec.get("evidence_receipt_ref")
            if isinstance(receipt_ref, str) and receipt_ref.strip():
                receipt = _find_by_id(svc_receipts, "receipt_id", receipt_ref)
                if receipt is None:
                    combined.add(
                        rel,
                        f"run[{run_id}].evidence_receipt_ref",
                        f"{receipt_ref!r} does not resolve to any EvidenceReceipt for "
                        f"service {service_id!r} — dangling evidence reference",
                    )
                elif receipt.get("conformance_run_ref") != run_id:
                    combined.add(
                        rel,
                        f"run[{run_id}].evidence_receipt_ref",
                        f"referenced EvidenceReceipt.conformance_run_ref "
                        f"{receipt.get('conformance_run_ref')!r} != this run's id {run_id!r} "
                        "— back-pointer mismatch",
                    )

    for receipt_path in _iter_jsonl_files(RECEIPTS_ROOT):
        rel = os.path.relpath(receipt_path, REPO_ROOT)
        service_id = os.path.splitext(os.path.basename(receipt_path))[0]
        try:
            receipts = _load_jsonl(receipt_path, id_field="receipt_id")
        except ValueError as exc:
            combined.add(rel, "<file>", f"malformed EvidenceReceipt log: {exc}")
            continue
        try:
            svc_runs = _runs_for_service(service_id) if service_id in SERVICE_IDS else []
        except ValueError:
            svc_runs = []
        for rec in receipts:
            receipt_id = rec.get("receipt_id")
            if not isinstance(receipt_id, str) or not receipt_id.strip():
                combined.add(rel, "receipt_id", "required string field is missing, null, or blank")
                continue
            # effect is UNKNOWN for an orphan receipt (it lives on the
            # referencing entry, not the receipt): pass the _EFFECT_UNKNOWN
            # sentinel so the effect-INDEPENDENT structural checks (required
            # fields, id types, cleanup_status enum, cleanup_confirmed
            # consistency) run while the per-effect readback/cleanup rules —
            # which genuinely need the entry's effect — are correctly skipped
            # rather than guessed. A None effect here would mis-classify
            # the receipt as a non-read effect and falsely reject a valid
            # read-operation receipt whose readback_result is null.
            _check_receipt_shape(combined, rel, receipt_id, rec, _EFFECT_UNKNOWN)
            # Back-pointer resolution: a receipt's conformance_run_ref must
            # resolve to a real run for this service.
            run_ref = rec.get("conformance_run_ref")
            if isinstance(run_ref, str) and run_ref.strip():
                run = _find_by_id(svc_runs, "run_id", run_ref)
                if run is None:
                    combined.add(
                        rel,
                        f"receipt[{receipt_id}].conformance_run_ref",
                        f"{run_ref!r} does not resolve to any ConformanceRun for "
                        f"service {service_id!r} — dangling evidence reference",
                    )
                elif run.get("evidence_receipt_ref") != receipt_id:
                    # One-to-one reciprocity (spec: ConformanceRun <-> EvidenceReceipt
                    # is a true one-to-one pairing). A second receipt pointing at an
                    # already-paired run whose evidence_receipt_ref names a DIFFERENT
                    # receipt is a broken pairing, not a valid reference.
                    combined.add(
                        rel,
                        f"receipt[{receipt_id}].conformance_run_ref",
                        f"referenced ConformanceRun.evidence_receipt_ref "
                        f"{run.get('evidence_receipt_ref')!r} != this receipt's id "
                        f"{receipt_id!r} — run<->receipt pairing is not one-to-one",
                    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _minimal_planned_entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "operation_id": "svc_do_thing",
        "operation_kind": "single_fetch",
        "provider": "example",
        "service_id": "github",
        "required": True,
        "category": "baseline_alignment",
        "source_status": "unverified",
        "source": {
            "source_kind": "not_yet_sourced",
            "source_id": "placeholder",
            "observed_at": None,
            "snapshot_ref": "NOT_YET_SOURCED",
        },
        "observed_at": "2026-09-14T00:00:00Z",
        "effect": "read",
        "input_schema": {"schema_ref": "https://example.com/schema-x", "schema_version": "1"},
        "output_schema": {"schema_ref": "https://example.com/schema-y", "schema_version": "1"},
        "tool_names": ["gh_do_thing"],
        "auth_modes": ["oauth_user"],
        "scopes": ["repo:read"],
        "account_types": ["personal"],
        "surfaces": ["chat"],
        "policy": {
            "platform_scope": None,
            "workspace_scope": None,
            "session_scope": None,
            "connection_scope": None,
            "provider_scope": None,
        },
        "adapter": dict(_ADAPTER_SENTINEL),
        "runner_version": "0.0.0",
        "verification_contract": None,
        "evidence_by_mode_surface_and_auth": [],
        "tested_sha": None,
        "merged_sha": None,
        "release_sha": None,
        "status": "planned",
        "last_reached_status": "planned",
        "blocker": None,
    }
    base.update(overrides)
    return base


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# All synthetic probes below use an entry whose operation_id=svc_do_thing and
# service_id=github (see _minimal_planned_entry's defaults), so every probe
# that is not specifically testing the path-match rule (#17/#18) uses THIS
# matching ref rather than an arbitrary "x" — otherwise the path-match check
# added by probe #17 would fail every other probe for an unrelated reason.
_MATCHING_REF = "docs/system-specs/connector-manifest/entries/github/svc_do_thing.json"


def self_test() -> None:
    probes = 0

    # 1. A minimal, honest planned entry is valid.
    probes += 1
    r = validate_entry(_minimal_planned_entry(), _MATCHING_REF)
    _assert(r.ok, f"minimal planned entry should validate clean, got {r.findings}")

    # 2. Missing a required top-level field is caught.
    probes += 1
    bad = _minimal_planned_entry()
    del bad["effect"]
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok and any(f.field == "effect" for f in r.findings), "missing effect must fail")

    # 3. Bad category enum value is caught.
    probes += 1
    bad = _minimal_planned_entry(category="not_a_real_category")
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "invalid category must fail")

    # 4. pagination required when operation_kind is list/search.
    probes += 1
    bad = _minimal_planned_entry(operation_kind="list", pagination=None)
    r = validate_entry(bad, _MATCHING_REF)
    _assert(
        not r.ok and any(f.field == "pagination" for f in r.findings),
        "list without pagination must fail",
    )

    probes += 1
    ok = _minimal_planned_entry(operation_kind="list", pagination="cursor")
    r = validate_entry(ok, _MATCHING_REF)
    _assert(r.ok, f"list WITH pagination should pass, got {r.findings}")

    # 5. retry required when effect is write-shaped.
    probes += 1
    bad = _minimal_planned_entry(effect="write", retry=None)
    r = validate_entry(bad, _MATCHING_REF)
    _assert(
        not r.ok and any(f.field == "retry" for f in r.findings), "write without retry must fail"
    )

    probes += 1
    ok = _minimal_planned_entry(
        effect="write",
        retry={"idempotency_class": "external_id_upsert", "detail": "uses request-id header"},
    )
    r = validate_entry(ok, _MATCHING_REF)
    _assert(r.ok, f"write WITH valid retry should pass, got {r.findings}")

    # 6. retry present on a read op is flagged.
    probes += 1
    bad = _minimal_planned_entry(
        effect="read", retry={"idempotency_class": "none_verify_by_readback", "detail": "n/a"}
    )
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "retry present on read-only effect must be flagged")

    # 7. snapshot_ref: official_docs requires https:// URL.
    probes += 1
    bad = _minimal_planned_entry(
        source={
            "source_kind": "official_docs",
            "source_id": "x",
            "observed_at": "2026-01-01T00:00:00Z",
            "snapshot_ref": "not-a-url",
        }
    )
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "official_docs with non-URL snapshot_ref must fail")

    probes += 1
    ok = _minimal_planned_entry(
        source={
            "source_kind": "official_docs",
            "source_id": "x",
            "observed_at": "2026-01-01T00:00:00Z",
            "snapshot_ref": "https://docs.github.com/en/rest",
        }
    )
    r = validate_entry(ok, _MATCHING_REF)
    _assert(r.ok, f"official_docs with https:// URL should pass, got {r.findings}")

    # 8. snapshot_ref: repo_path requires an in-repo path that exists.
    probes += 1
    bad = _minimal_planned_entry(
        source={
            "source_kind": "repo_path",
            "source_id": "x",
            "observed_at": "2026-01-01T00:00:00Z",
            "snapshot_ref": "docs/system-specs/connector-manifest/DOES_NOT_EXIST_xyz.md",
        }
    )
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "repo_path snapshot_ref pointing at a nonexistent file must fail")

    probes += 1
    ok = _minimal_planned_entry(
        source={
            "source_kind": "repo_path",
            "source_id": "x",
            "observed_at": "2026-01-01T00:00:00Z",
            "snapshot_ref": "AGENTS.md",
        }
    )
    r = validate_entry(ok, _MATCHING_REF)
    _assert(r.ok, f"repo_path snapshot_ref pointing at a real file should pass, got {r.findings}")

    # 9b. The evidence-catalog artifact itself must be resolvable — this is
    # required by the snapshot_ref resolution contract (the catalog was
    # cited as authoritative but had no in-repo home). A snapshot_ref citing
    # it by its now-real, mirrored path must pass. code-audit.json and
    # contract-and-dag.md are deliberately NOT in this list: neither is
    # mirrored (nothing this repo's validator/tests/spec consumes them —
    # see the campaign-evidence/README.md's own explanation).
    probes += 1
    ok = _minimal_planned_entry(
        source={
            "source_kind": "repo_path",
            "source_id": "x",
            "observed_at": "2026-01-01T00:00:00Z",
            "snapshot_ref": (
                "docs/system-specs/connector-manifest/campaign-evidence/catalog-evidence.json"
            ),
        }
    )
    r = validate_entry(ok, _MATCHING_REF)
    _assert(r.ok, f"evidence-catalog artifact should resolve, got {r.findings}")

    # 9. Out-of-repo path referencing the private campaign workspace is rejected.
    probes += 1
    bad = _minimal_planned_entry(
        source={
            "source_kind": "search_snippet_corroborated",
            "source_id": "x",
            "observed_at": "2026-01-01T00:00:00Z",
            "snapshot_ref": "/mnt/external-campaign-workspace/catalog-evidence.json",
        }
    )
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "an absolute out-of-repo path must be rejected as non-resolvable")

    # 10. status=code_complete requires verification_contract + tested_sha.
    probes += 1
    bad = _minimal_planned_entry(status="code_complete", last_reached_status="code_complete")
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "code_complete without verification_contract/tested_sha must fail")

    # 11. blocked requires last_reached_status != blocked and a populated blocker.
    probes += 1
    bad = _minimal_planned_entry(status="blocked", last_reached_status="blocked")
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "last_reached_status must never itself be blocked")

    probes += 1
    bad = _minimal_planned_entry(status="blocked", last_reached_status="implementing", blocker=None)
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "status=blocked requires a populated blocker object")

    probes += 1
    ok = _minimal_planned_entry(
        status="blocked",
        last_reached_status="implementing",
        blocker={
            "reason": "BLOCKED_POLICY",
            "owner": "campaign owner",
            "unblock_action": "resolve model policy",
        },
        auth_modes=["oauth_user"],
        account_types=["personal"],
        surfaces=["chat"],
        evidence_by_mode_surface_and_auth=[
            {
                "auth_mode": "oauth_user",
                "account_type": "personal",
                "surface": "chat",
                "applicable": True,
                "exclusion_reason": None,
                "verification_contract_ref": None,
                "status": "implementing",
                "last_reached_status": "implementing",
            }
        ],
    )
    r = validate_entry(ok, _MATCHING_REF)
    _assert(r.ok, f"a properly-populated blocked entry should pass, got {r.findings}")

    # 12. evidence_by_mode_surface_and_auth must be TOTAL once status leaves planned.
    probes += 1
    bad = _minimal_planned_entry(
        status="implementing",
        last_reached_status="implementing",
        auth_modes=["oauth_user", "service_to_service"],
        account_types=["personal"],
        surfaces=["chat"],
        evidence_by_mode_surface_and_auth=[
            {
                "auth_mode": "oauth_user",
                "account_type": "personal",
                "surface": "chat",
                "applicable": True,
                "exclusion_reason": None,
                "verification_contract_ref": None,
                "status": "implementing",
                "last_reached_status": "implementing",
            }
        ],
    )
    r = validate_entry(bad, _MATCHING_REF)
    _assert(
        not r.ok and any(f.field == "evidence_by_mode_surface_and_auth" for f in r.findings),
        "a matrix missing the second auth_mode's row must fail totality",
    )

    probes += 1
    ok = _minimal_planned_entry(
        status="implementing",
        last_reached_status="implementing",
        auth_modes=["oauth_user"],
        account_types=["personal"],
        surfaces=["chat"],
        evidence_by_mode_surface_and_auth=[
            {
                "auth_mode": "oauth_user",
                "account_type": "personal",
                "surface": "chat",
                "applicable": True,
                "exclusion_reason": None,
                "verification_contract_ref": None,
                "status": "implementing",
                "last_reached_status": "implementing",
            }
        ],
    )
    r = validate_entry(ok, _MATCHING_REF)
    _assert(r.ok, f"a total 1x1x1 matrix should pass, got {r.findings}")

    # 13. duplicate matrix rows for the same triple are flagged.
    probes += 1
    dup_row = {
        "auth_mode": "oauth_user",
        "account_type": "personal",
        "surface": "chat",
        "applicable": True,
        "exclusion_reason": None,
        "verification_contract_ref": None,
        "status": "implementing",
        "last_reached_status": "implementing",
    }
    bad = _minimal_planned_entry(
        status="implementing",
        last_reached_status="implementing",
        auth_modes=["oauth_user"],
        account_types=["personal"],
        surfaces=["chat"],
        evidence_by_mode_surface_and_auth=[dup_row, dict(dup_row)],
    )
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "duplicate (auth_mode, account_type, surface) rows must fail")

    # 14. applicable=false requires a non-null exclusion_reason.
    probes += 1
    bad = _minimal_planned_entry(
        status="implementing",
        last_reached_status="implementing",
        auth_modes=["oauth_user"],
        account_types=["personal"],
        surfaces=["chat"],
        evidence_by_mode_surface_and_auth=[
            {
                "auth_mode": "oauth_user",
                "account_type": "personal",
                "surface": "chat",
                "applicable": False,
                "exclusion_reason": None,
                "verification_contract_ref": None,
                "status": "planned",
                "last_reached_status": "planned",
            }
        ],
    )
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "applicable=false without exclusion_reason must fail")

    # 15. Cross-file: verification_contract pointing at a run/receipt that
    #     does not exist on disk must fail (integration-style probe using a
    #     nonexistent service_id-scoped jsonl, so this cannot find a real
    #     record by construction).
    probes += 1
    bad = _minimal_planned_entry(
        status="code_complete",
        last_reached_status="code_complete",
        tested_sha="0" * 40,
        verification_contract={
            "run_ref": "run_does_not_exist_xyz",
            "receipt_ref": "receipt_does_not_exist_xyz",
        },
    )
    r = validate_entry(bad, "entries/github/svc_do_thing.json")
    _assert(
        not r.ok and any("does not resolve" in f.message for f in r.findings),
        "a verification_contract pointing at a nonexistent run must fail",
    )

    # 16. last_reached_status disagreeing with status when not blocked fails.
    probes += 1
    bad = _minimal_planned_entry(status="implementing", last_reached_status="planned")
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "last_reached_status must equal status when not blocked")

    # 17. operation_id/service_id must match the file's own path.
    probes += 1
    ok = _minimal_planned_entry()
    r = validate_entry(ok, "docs/system-specs/connector-manifest/entries/somewhere/wrong.json")
    _assert(
        not r.ok, "an entry whose operation_id/service_id disagree with its file path must fail"
    )

    probes += 1
    ok = _minimal_planned_entry()
    r = validate_entry(ok, "docs/system-specs/connector-manifest/entries/github/svc_do_thing.json")
    _assert(
        r.ok,
        f"an entry whose file path matches operation_id/service_id should pass, got {r.findings}",
    )

    # 19. A relative repo_path snapshot_ref that
    # traverses out of the repo via '..' segments must be rejected — the
    # isabs() guard alone does not catch this, since os.path.join +
    # os.path.exists resolves '..' through the real filesystem.
    probes += 1
    bad = _minimal_planned_entry(
        source={
            "source_kind": "repo_path",
            "source_id": "x",
            "observed_at": "2026-01-01T00:00:00Z",
            "snapshot_ref": "../../../../../../../etc/passwd",
        }
    )
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "a relative path that resolves outside the repo root must be rejected")

    # 20. An enum field carrying an unhashable JSON type
    # (a list or dict, e.g. category: []) must produce a Finding, never crash
    # the gate with TypeError from `value not in <frozenset>`.
    probes += 1
    bad = _minimal_planned_entry(category=[])
    r = validate_entry(bad, _MATCHING_REF)  # must not raise
    _assert(not r.ok, "an unhashable category value must fail cleanly, not crash")

    probes += 1
    bad = _minimal_planned_entry(
        source={"source_kind": [], "source_id": "x", "observed_at": None, "snapshot_ref": "x"}
    )
    r = validate_entry(bad, _MATCHING_REF)  # must not raise
    _assert(not r.ok, "an unhashable source_kind value must fail cleanly, not crash")

    probes += 1
    bad = _minimal_planned_entry(effect="write", retry={"idempotency_class": [], "detail": "x"})
    r = validate_entry(bad, _MATCHING_REF)  # must not raise
    _assert(not r.ok, "an unhashable idempotency_class value must fail cleanly, not crash")

    probes += 1
    bad = _minimal_planned_entry(status=[], last_reached_status=[])
    r = validate_entry(bad, _MATCHING_REF)  # must not raise
    _assert(not r.ok, "an unhashable status value must fail cleanly, not crash")

    # 21. schema_ref was checked for presence only,
    # never resolvability, unlike snapshot_ref — same defect one field over.
    probes += 1
    ok = _minimal_planned_entry(
        input_schema={"schema_ref": "https://docs.github.com/en/rest/issues", "schema_version": "1"}
    )
    r = validate_entry(ok, _MATCHING_REF)
    _assert(r.ok, f"an https:// schema_ref should pass, got {r.findings}")

    probes += 1
    ok = _minimal_planned_entry(
        input_schema={"schema_ref": "AGENTS.md#section", "schema_version": "1"}
    )
    r = validate_entry(ok, _MATCHING_REF)
    _assert(r.ok, f"an existing in-repo schema_ref with a #fragment should pass, got {r.findings}")

    probes += 1
    bad = _minimal_planned_entry(
        input_schema={"schema_ref": "../../../../../../../etc/passwd", "schema_version": "1"}
    )
    r = validate_entry(bad, _MATCHING_REF)
    _assert(not r.ok, "a traversal schema_ref that resolves outside the repo root must be rejected")

    # 22. policy.*_scope membership against the real governance SCOPE_CATALOG
    # (read via AST from src/kiro_crew/platform/governance.py): a real catalog
    # key passes, an unknown name is rejected, null is fine.
    _catalog = _governance_scope_catalog()
    if _catalog:
        _real_key = sorted(_catalog)[0]
        probes += 1
        ok = _minimal_planned_entry(
            policy={
                "platform_scope": _real_key,
                "workspace_scope": None,
                "session_scope": None,
                "connection_scope": None,
                "provider_scope": None,
            }
        )
        r = validate_entry(ok, _MATCHING_REF)
        _assert(r.ok, f"a real SCOPE_CATALOG key {_real_key!r} should pass, got {r.findings}")

        probes += 1
        bad = _minimal_planned_entry(
            policy={
                "platform_scope": "definitely.not.a.catalog.key",
                "workspace_scope": None,
                "session_scope": None,
                "connection_scope": None,
                "provider_scope": None,
            }
        )
        r = validate_entry(bad, _MATCHING_REF)
        _assert(
            not r.ok and any("SCOPE_CATALOG" in f.message for f in r.findings),
            "an unknown policy scope not in SCOPE_CATALOG must be rejected via membership",
        )

    print(f"check_connector_manifest.py self-test: {probes} probes passed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="run the self-test suite and exit")
    parser.add_argument(
        "--entry", action="append", default=None, help="validate only this entry file (repeatable)"
    )
    args = parser.parse_args(argv)

    if args.test:
        try:
            self_test()
        except AssertionError as exc:
            print(f"SELF-TEST FAILURE: {exc}", file=sys.stderr)
            return 1
        return 0

    result = run_scan(args.entry)
    if result.ok:
        n = len(args.entry) if args.entry else len(_iter_entry_files())
        print(
            f"check_connector_manifest.py: {n} entr{'y' if n == 1 else 'ies'} validated, 0 findings"
        )
        return 0

    for finding in result.findings:
        print(str(finding), file=sys.stderr)
    print(
        f"check_connector_manifest.py: {len(result.findings)} finding(s) across the manifest",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
