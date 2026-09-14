"""Unit and integration tests for scripts/check_connector_manifest.py.

Two layers, matching the gate's own two jobs:

- **Negative/structural tests** (`TestFieldRules`, `TestCrossFileConsistency`)
  exercise `validate_entry` directly against synthetic dicts, so a single bad
  field is pinned without needing a real file on disk. This is where a
  schema-violating input is proven to actually fail, not just assumed to.
- **Fixture/integration tests** (`TestFixtureEntries`) run the gate's own
  `--test`/scan entry points against the two real fixture entries this PR
  ships under `docs/system-specs/connector-manifest/entries/github/`, so the
  documented examples are proven to stay valid as the schema evolves —
  a fixture that silently rotted would defeat the point of shipping one.

The self-test suite inside the script itself (`check_connector_manifest.py
--test`) is the authoritative probe-by-probe pin; this file additionally
runs it as a subprocess so a CI failure surfaces in pytest's own summary
rather than only in a separate gate step.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "scripts", "check_connector_manifest.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_connector_manifest", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_connector_manifest"] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


def _entry(**overrides):
    return gate._minimal_planned_entry(**overrides)


_REF = gate._MATCHING_REF


# ---------------------------------------------------------------------------
# Field-level rules, each pinned independently of the script's own self-test
# (belt-and-suspenders: a regression that breaks the self-test's own
# assertions AND this file's independent expectations is caught twice).
# ---------------------------------------------------------------------------


class TestFieldRules:
    def test_minimal_planned_entry_is_valid(self):
        result = gate.validate_entry(_entry(), _REF)
        assert result.ok, result.findings

    @pytest.mark.parametrize(
        "field_name",
        [
            "operation_id",
            "operation_kind",
            "provider",
            "service_id",
            "category",
            "source_status",
            "source",
            "effect",
            "policy",
            "adapter",
            "status",
            "last_reached_status",
        ],
    )
    def test_missing_required_field_fails(self, field_name):
        entry = _entry()
        del entry[field_name]
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == field_name for f in result.findings)

    def test_unknown_service_id_rejected(self):
        entry = _entry(service_id="not_a_real_service")
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    @pytest.mark.parametrize(
        "field_name",
        [
            "operation_id",
            "operation_kind",
            "provider",
            "service_id",
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
        ],
    )
    def test_required_field_present_but_null_fails(self, field_name):
        """A key that IS present with a JSON null value passed the old
        "field_name not in entry" presence check while still being exactly
        the missing-value case that check exists to catch, for every field
        whose own type never legitimately includes null (unlike
        verification_contract/tested_sha/merged_sha/release_sha, which ARE
        legitimately null at early rungs and are covered separately)."""
        entry = _entry(**{field_name: None})
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == field_name for f in result.findings)

    def test_verification_contract_null_at_planned_still_passes(self):
        """The rung-conditional-null fields must NOT be swept into the
        present-but-null rejection — a planned entry legitimately leaves
        them null."""
        entry = _entry(
            verification_contract=None, tested_sha=None, merged_sha=None, release_sha=None
        )
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings

    def test_unknown_category_rejected(self):
        entry = _entry(category="not_a_real_category")
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_pagination_required_for_list_and_search(self):
        for kind in ("list", "search"):
            entry = _entry(operation_kind=kind, pagination=None)
            result = gate.validate_entry(entry, _REF)
            assert not result.ok, f"operation_kind={kind} without pagination should fail"

    def test_pagination_not_required_for_single_fetch(self):
        entry = _entry(operation_kind="single_fetch", pagination=None)
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings

    @pytest.mark.parametrize("effect", ["write", "delete", "share", "external_send", "admin"])
    def test_retry_required_for_write_shaped_effects(self, effect):
        entry = _entry(effect=effect, retry=None)
        result = gate.validate_entry(entry, _REF)
        assert not result.ok, f"effect={effect} without retry should fail"

    @pytest.mark.parametrize("effect", ["read", "billable"])
    def test_retry_not_required_for_read_or_billable(self, effect):
        entry = _entry(effect=effect, retry=None)
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings

    def test_retry_idempotency_class_enum_enforced(self):
        entry = _entry(effect="write", retry={"idempotency_class": "bogus", "detail": "x"})
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    @pytest.mark.parametrize(
        "source_kind,snapshot_ref,expect_ok",
        [
            ("official_docs", "https://docs.github.com/en/rest", True),
            ("official_docs", "not-a-url", False),
            ("format_spec", "https://www.rfc-editor.org/rfc/rfc4180", True),
            ("format_spec", "AGENTS.md", False),
            ("repo_path", "AGENTS.md", True),
            ("repo_path", "docs/does/not/exist.md", False),
            ("repo_path", "/etc/passwd", False),
            ("user_stated", "AGENTS.md", True),
            ("search_snippet_corroborated", "AGENTS.md", True),
        ],
    )
    def test_snapshot_ref_resolution_contract(self, source_kind, snapshot_ref, expect_ok):
        # The spec mandates two source_kind->source_status pairings; set the
        # required status so this test exercises snapshot_ref resolution, not
        # the (separately-tested) pairing rule.
        status_for_kind = {"user_stated": "user_required", "not_yet_sourced": "unverified"}
        entry = _entry(
            source={
                "source_kind": source_kind,
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": snapshot_ref,
            },
            source_status=status_for_kind.get(source_kind, "official_baseline"),
        )
        result = gate.validate_entry(entry, _REF)
        assert result.ok is expect_ok, result.findings

    def test_snapshot_ref_rejects_absolute_path_outside_repo(self):
        """The exact regression the S1 orphaned finding was about: a
        snapshot_ref pointing into the campaign's private, out-of-repo
        workspace must not validate."""
        entry = _entry(
            source={
                "source_kind": "search_snippet_corroborated",
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "/mnt/external-campaign-workspace/catalog-evidence.json",
            }
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_snapshot_ref_rejects_relative_traversal_outside_repo(self):
        """A relative-looking
        repo_path snapshot_ref that traverses out of the repo via '..'
        segments (e.g. '../../../../etc/passwd') is NOT caught by the
        isabs() guard alone, because os.path.join + os.path.exists happily
        resolves '..' through the real filesystem. The fix requires the
        REALPATH to stay under the repo root, checked via os.path.commonpath
        — not just that the string itself lacks a leading '/'."""
        entry = _entry(
            source={
                "source_kind": "repo_path",
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "../../../../../../../etc/passwd",
            }
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok, "a relative path that resolves outside the repo root must be rejected"

    def test_snapshot_ref_rejects_hostless_https_literal(self):
        """A bare 'https://' string (or a host-free variant like
        'https:///x') satisfies a str.startswith("https://") prefix check
        while citing nothing a reader could ever resolve."""
        entry = _entry(
            source={
                "source_kind": "official_docs",
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "https://",
            }
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_snapshot_ref_rejects_nul_byte_path_without_crashing(self):
        """A repo_path snapshot_ref containing an embedded NUL byte is
        legal JSON but not resolvable by the filesystem — os.path.realpath
        must not be allowed to raise ValueError and crash the whole gate;
        the entry must instead fail with an ordinary Finding."""
        entry = _entry(
            source={
                "source_kind": "repo_path",
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "AGENTS.md\x00",
            }
        )
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_schema_ref_rejects_hostless_https_literal(self):
        entry = _entry(
            input_schema={"schema_version": "1", "schema_ref": "https://"},
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_schema_ref_rejects_nul_byte_path_without_crashing(self):
        entry = _entry(
            input_schema={"schema_version": "1", "schema_ref": "AGENTS.md\x00"},
        )
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    @pytest.mark.parametrize(
        "field_name,bad_value",
        [
            ("category", []),
            ("category", {}),
            ("source_status", []),
            ("effect", []),
            ("operation_kind", []),
        ],
    )
    def test_enum_fields_never_crash_on_unhashable_json_types(self, field_name, bad_value):
        """`value not in allowed` on a frozenset raises
        TypeError when value is a list/dict (unhashable), instead of cleanly
        failing validation. A malformed manifest entry must produce a
        Finding, never crash the whole gate."""
        entry = _entry(**{field_name: bad_value})
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_source_kind_unhashable_type_never_crashes(self):
        entry = _entry(
            source={
                "source_kind": [],
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "x",
            }
        )
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_retry_idempotency_class_unhashable_type_never_crashes(self):
        entry = _entry(effect="write", retry={"idempotency_class": [], "detail": "x"})
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_status_unhashable_type_never_crashes(self):
        entry = _entry(status=[], last_reached_status=[])
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_operation_kind_unhashable_type_never_crashes_pagination_check(self):
        entry = _entry(operation_kind=[])
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_evidence_matrix_row_status_unhashable_type_never_crashes(self):
        entry = _entry(
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
                    "status": [],
                    "last_reached_status": [],
                }
            ],
        )
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_not_yet_sourced_placeholder_is_exempt_from_existence_check(self):
        entry = _entry()  # default source is not_yet_sourced with a placeholder
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings

    def test_evidence_catalog_artifact_is_resolvable_in_repo(self):
        """The evidence catalog must have a
        named, in-repo home that a reader with only this repo can open.
        This path points at the committed, distilled copy, not the private
        campaign workspace. code-audit.json and contract-and-dag.md are
        deliberately excluded — neither is mirrored (zero consumers in
        this repo's own validator/tests/spec; see
        campaign-evidence/README.md)."""
        entry = _entry(
            source={
                "source_kind": "repo_path",
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": (
                    "docs/system-specs/connector-manifest/campaign-evidence/"
                    "catalog-evidence.json"
                ),
            }
        )
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings

    def test_code_audit_json_is_deliberately_not_mirrored(self):
        """Pins the deletion decision: code-audit.json was removed as a
        zero-consumer artifact — a
        snapshot_ref citing it must fail, not silently resolve, so a future
        re-add of the file without updating this test is caught."""
        entry = _entry(
            source={
                "source_kind": "repo_path",
                "source_id": "x",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "docs/system-specs/connector-manifest/campaign-evidence/code-audit.json",
            }
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    @pytest.mark.parametrize(
        "schema_ref,expect_ok",
        [
            ("https://docs.github.com/en/rest/issues", True),
            ("not-a-url-and-not-in-repo.md", False),
            ("AGENTS.md", True),
            ("AGENTS.md#some-fragment", True),
            ("docs/does/not/exist.md", False),
            ("/etc/passwd", False),
            ("../../../../../../../etc/passwd", False),
        ],
    )
    def test_schema_ref_resolution_contract(self, schema_ref, expect_ok):
        """schema_ref was only checked for presence,
        never resolvability, unlike snapshot_ref — the same defect class one
        field over. Applies the identical https://-or-in-repo-path rule,
        including the realpath/commonpath containment check (the same fix
        as the F1 path-traversal finding)."""
        entry = _entry(
            input_schema={"schema_ref": schema_ref, "schema_version": "1"},
        )
        result = gate.validate_entry(entry, _REF)
        assert result.ok is expect_ok, result.findings

    def test_schema_ref_blank_string_fails(self):
        """An empty/whitespace-only schema_ref is a present, string-typed
        value that the presence check ("sub not in schema") did not catch,
        and _check_schema_ref itself silently returned on — treating a
        blank citation as already handled elsewhere when nothing actually
        flagged it."""
        entry = _entry(
            input_schema={"schema_ref": "   ", "schema_version": "1"},
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_schema_ref_null_fails(self):
        entry = _entry(
            input_schema={"schema_ref": None, "schema_version": "1"},
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def _policy(self, **overrides):
        base = {
            "platform_scope": None,
            "workspace_scope": None,
            "session_scope": None,
            "connection_scope": None,
            "provider_scope": None,
        }
        base.update(overrides)
        return base

    def test_policy_scope_real_catalog_key_passes(self):
        """A non-null policy scope naming an ACTUAL governance SCOPE_CATALOG
        key (read live from governance.py, not hardcoded here) is accepted."""
        catalog = gate._governance_scope_catalog()
        assert catalog, "SCOPE_CATALOG must be readable for this test to be meaningful"
        # exercise both a dotted key and a flat key from the real catalog
        dotted = next((k for k in sorted(catalog) if "." in k), None)
        flat = next((k for k in sorted(catalog) if "." not in k), None)
        for key in (k for k in (dotted, flat) if k):
            entry = _entry(policy=self._policy(platform_scope=key))
            result = gate.validate_entry(entry, _REF)
            assert result.ok, (key, result.findings)

    def test_policy_scope_unknown_name_is_rejected(self):
        """A non-null policy scope that is NOT a SCOPE_CATALOG member is
        rejected (membership, not just shape). 'connection.github.read' is a
        well-formed dotted token but is not a catalog key."""
        catalog = gate._governance_scope_catalog()
        assert catalog and "connection.github.read" not in catalog
        entry = _entry(policy=self._policy(connection_scope="connection.github.read"))
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(
            f.field == "policy.connection_scope" and "SCOPE_CATALOG" in f.message
            for f in result.findings
        ), result.findings

    def test_policy_scope_null_passes(self):
        entry = _entry(policy=self._policy(connection_scope=None))
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings

    def test_policy_scope_blank_string_fails(self):
        entry = _entry(policy=self._policy(connection_scope="   "))
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == "policy.connection_scope" for f in result.findings)

    def test_policy_scope_bare_token_not_in_catalog_is_rejected(self):
        """A bare word that is not a catalog key is rejected as a non-member
        (membership check), not merely as a shape violation."""
        entry = _entry(policy=self._policy(platform_scope="notacatalogkey"))
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == "policy.platform_scope" for f in result.findings)

    def test_policy_scope_non_string_fails(self):
        entry = _entry(policy=self._policy(session_scope=42))
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == "policy.session_scope" for f in result.findings)

    def test_code_complete_requires_verification_contract_and_tested_sha(self):
        entry = _entry(status="code_complete", last_reached_status="code_complete")
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_merged_requires_merged_sha(self):
        entry = _entry(
            status="merged",
            last_reached_status="merged",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "r1", "receipt_ref": "e1"},
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == "status" for f in result.findings)

    def test_blocked_requires_populated_blocker(self):
        entry = _entry(status="blocked", last_reached_status="implementing", blocker=None)
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_last_reached_status_can_never_be_blocked(self):
        entry = _entry(status="blocked", last_reached_status="blocked")
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_last_reached_status_must_equal_status_when_not_blocked(self):
        entry = _entry(status="implementing", last_reached_status="planned")
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_evidence_matrix_must_be_total_once_status_leaves_planned(self):
        entry = _entry(
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
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == "evidence_by_mode_surface_and_auth" for f in result.findings)

    def test_evidence_matrix_applicable_false_requires_exclusion_reason(self):
        entry = _entry(
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
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_operation_id_service_id_must_match_file_path(self):
        entry = _entry()
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/gmail/wrong.json"
        )
        assert not result.ok


# ---------------------------------------------------------------------------
# Cross-file consistency: verification_contract <-> ConformanceRun <->
# EvidenceReceipt, using temporary runs/receipts files so this suite never
# depends on (or corrupts) the real fixture jsonl files.
# ---------------------------------------------------------------------------


class TestCrossFileConsistency:
    @pytest.fixture(autouse=True)
    def _isolated_manifest_root(self, tmp_path, monkeypatch):
        runs_dir = tmp_path / "runs"
        receipts_dir = tmp_path / "receipts"
        runs_dir.mkdir()
        receipts_dir.mkdir()
        monkeypatch.setattr(gate, "RUNS_ROOT", str(runs_dir))
        monkeypatch.setattr(gate, "RECEIPTS_ROOT", str(receipts_dir))
        self.runs_dir = runs_dir
        self.receipts_dir = receipts_dir

    def _write_run(self, service_id, **fields):
        base = {
            "run_id": "run_1",
            "operation_id": "op_1",
            "account_binding_ref": "fixture",
            "auth_mode": "oauth_user",
            "account_type": "personal",
            "surface": "chat",
            "tested_sha": "a" * 40,
            "adapter_version": "0.1.0",
            "input_schema_version": "1.0.0",
            "output_schema_version": "1.0.0",
            "runner_version": "0.1.0",
            "executed_at": "2026-01-01T00:00:00Z",
            "request_shape_hash": "sha256:0",
            "response_summary": {
                "fields_present": [],
                "types_matched": True,
                "unexpected_fields": [],
            },
            "verdict": "pass",
            "evidence_receipt_ref": "receipt_1",
        }
        base.update(fields)
        path = self.runs_dir / f"{service_id}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(base) + "\n")
        return base

    def _write_receipt(self, service_id, **fields):
        base = {
            "receipt_id": "receipt_1",
            "conformance_run_ref": "run_1",
            "claim": "test claim",
            "runtime_verified": True,
            "readback_result": None,
            "cleanup_confirmed": False,
            "cleanup_status": "not_applicable",
            "negative_test_refs": ["neg_1"],
        }
        base.update(fields)
        path = self.receipts_dir / f"{service_id}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(base) + "\n")
        return base

    def test_valid_three_way_reference_passes(self):
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert result.ok, result.findings

    def test_contract_verified_run_coordinates_must_match_a_declared_applicable_cell(self):
        """the spec requires contract_verified to be
        certified against a (auth_mode, account_type, surface) combination
        the entry ITSELF declares — a run for an undeclared/mismatched
        coordinate would satisfy an entry-level check that only verifies
        operation_id, even though the entry's own matrix never declared
        that coordinate applicable."""
        self._write_run(
            "github",
            operation_id="op_1",
            auth_mode="service_to_service",  # entry's matrix below never declares this
            account_type="personal",
            surface="chat",
        )
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="contract_verified",
            last_reached_status="contract_verified",
            tested_sha="a" * 40,
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
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
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_contract_verified_run_coordinates_matching_a_declared_cell_passes(self):
        self._write_run(
            "github",
            operation_id="op_1",
            auth_mode="oauth_user",
            account_type="personal",
            surface="chat",
        )
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="contract_verified",
            last_reached_status="contract_verified",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            input_schema={"schema_ref": "https://example.com/x", "schema_version": "1.0.0"},
            output_schema={"schema_ref": "https://example.com/y", "schema_version": "1.0.0"},
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert result.ok, result.findings
        assert result.ok, result.findings

    def test_run_ref_pointing_at_nothing_fails(self):
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "does_not_exist", "receipt_ref": "does_not_exist"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_back_pointer_mismatch_fails(self):
        self._write_run("github", operation_id="op_1", evidence_receipt_ref="receipt_1")
        self._write_receipt("github", conformance_run_ref="some_other_run")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_run_operation_id_mismatch_fails(self):
        self._write_run("github", operation_id="a_different_operation")
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_non_passing_run_never_promotes_status(self):
        self._write_run("github", operation_id="op_1", verdict="fail")
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_runtime_verified_false_never_promotes_status(self):
        self._write_run("github", operation_id="op_1")
        self._write_receipt("github", runtime_verified=False)
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_stale_tested_sha_flagged(self):
        """Immutable ref binding: once the entry's own tested_sha has moved
        past what the certifying run recorded, the pointer is stale."""
        self._write_run("github", operation_id="op_1", tested_sha="a" * 40)
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="b" * 40,  # moved on since the run executed
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any("STALE" in f.message for f in result.findings)

    def test_stale_input_schema_version_flagged(self):
        """the spec names FIVE fields that
        must match the certifying run, not just tested_sha/runner_version/
        adapter.version — input_schema.schema_version and
        output_schema.schema_version are the other two."""
        self._write_run("github", operation_id="op_1", input_schema_version="1.0.0")
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            input_schema={"schema_ref": "x", "schema_version": "2.0.0"},  # moved on
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any("STALE" in f.message and "input_schema" in f.field for f in result.findings)

    def test_stale_output_schema_version_flagged(self):
        self._write_run("github", operation_id="op_1", output_schema_version="1.0.0")
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            output_schema={"schema_ref": "y", "schema_version": "3.0.0"},  # moved on
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any("STALE" in f.message and "output_schema" in f.field for f in result.findings)

    def test_release_verified_requires_run_tested_sha_equals_release_sha(self):
        self._write_run("github", operation_id="op_1", tested_sha="a" * 40)
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="release_verified",
            last_reached_status="release_verified",
            tested_sha="a" * 40,
            merged_sha="a" * 40,
            release_sha="c" * 40,  # different from the run's tested_sha
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_matrix_cell_wrong_coordinate_never_promotes_cell(self):
        """A run recorded for a DIFFERENT auth_mode/account_type/surface must
        never promote this cell, even though it is a genuinely passing run
        for the same operation_id — the four-coordinate equality rule."""
        self._write_run(
            "github",
            operation_id="op_1",
            auth_mode="service_to_service",  # cell below declares oauth_user
            account_type="personal",
            surface="chat",
        )
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
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
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_code_complete_does_not_require_a_passing_verdict(self):
        """code_complete's own spec text says it "has not yet
        run a live ConformanceRun" — requiring verdict==pass at this rung
        would reject the spec's own legitimate rung. The verdict/
        runtime_verified check applies from contract_verified onward only."""
        self._write_run("github", operation_id="op_1", verdict="fail", runner_version="0.1.0")
        self._write_receipt("github", runtime_verified=False)
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            input_schema={"schema_ref": "https://example.com/x", "schema_version": "1.0.0"},
            output_schema={"schema_ref": "https://example.com/y", "schema_version": "1.0.0"},
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "code_complete",
                    "last_reached_status": "code_complete",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert result.ok, result.findings

    def test_contract_verified_still_requires_a_passing_verdict(self):
        """The other half of the same fix: contract_verified (and later)
        still requires verdict==pass — only code_complete is exempt."""
        self._write_run("github", operation_id="op_1", verdict="fail")
        self._write_receipt("github", runtime_verified=False)
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="contract_verified",
            last_reached_status="contract_verified",
            tested_sha="a" * 40,
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_malformed_jsonl_record_is_a_finding_not_a_crash(self):
        """a JSONL line that decodes to a non-object (a bare
        scalar) must not crash the gate with AttributeError on the first
        lookup — it must be reported as a Finding."""
        path = self.runs_dir / "github.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("42\n")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )  # must not raise
        assert not result.ok

    def test_duplicate_run_id_is_a_finding_not_a_silent_first_match(self):
        """two ConformanceRun records sharing one run_id must
        be rejected as an ambiguous evidence graph, not silently resolved
        to whichever one is first in the file."""
        self._write_run("github", run_id="dup_run", operation_id="op_1")
        self._write_run("github", run_id="dup_run", operation_id="op_2")
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "dup_run", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )  # must not raise
        assert not result.ok

    def test_unhashable_axis_element_in_matrix_is_a_finding_not_a_crash(self):
        """auth_modes/account_types/surfaces containing an
        unhashable element (a list) must not crash set construction — it
        must be reported per-element as a Finding."""
        entry = _entry(
            status="implementing",
            last_reached_status="implementing",
            auth_modes=[[]],
            account_types=["personal"],
            surfaces=["chat"],
            evidence_by_mode_surface_and_auth=[],
        )
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_unhashable_row_coordinate_in_matrix_is_a_finding_not_a_crash(self):
        entry = _entry(
            status="implementing",
            last_reached_status="implementing",
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": [],
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
        result = gate.validate_entry(entry, _REF)  # must not raise
        assert not result.ok

    def test_unhashable_jsonl_id_is_a_finding_not_a_crash(self):
        """record_id in seen_ids hashes record_id, so a JSONL
        record whose id field is itself unhashable (run_id: []) must not
        crash the gate."""
        path = self.runs_dir / "github.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_id": []}) + "\n")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )  # must not raise
        assert not result.ok

    def test_applicable_row_outside_declared_cross_product_never_certifies(self):
        """an applicable=true row whose OWN coordinate is not
        even a member of the entry's own declared auth_modes x
        account_types x surfaces cross-product must never count as a
        legitimate cell a run can certify against — a rogue row claiming
        applicable=true for an undeclared coordinate is not itself proof
        that coordinate is real."""
        self._write_run(
            "github",
            operation_id="op_1",
            auth_mode="rogue_undeclared_mode",
            account_type="personal",
            surface="chat",
        )
        self._write_receipt("github")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="contract_verified",
            last_reached_status="contract_verified",
            tested_sha="a" * 40,
            auth_modes=["oauth_user"],  # rogue_undeclared_mode is NOT here
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "rogue_undeclared_mode",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_merged_entry_still_requires_live_verified_completeness(self):
        """the completeness check was gated solely on
        effective_status == "live_verified", so a merged (or
        release_verified) entry with an applicable cell still stuck at
        implementing skipped the check entirely, even though merged's own
        per-rung requirements are cumulative on top of live_verified's."""
        entry = _entry(
            status="merged",
            last_reached_status="merged",
            tested_sha="a" * 40,
            merged_sha="b" * 40,
            verification_contract={"run_ref": "x", "receipt_ref": "y"},
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
                    "status": "implementing",  # not contract_verified or later
                    "last_reached_status": "implementing",
                }
            ],
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_receipt_cleanup_confirmed_disagreeing_with_cleanup_status_fails(self):
        """The spec derives cleanup_confirmed from cleanup_status
        (true iff 'confirmed') — a receipt where the two disagree is
        malformed and was never checked."""
        self._write_run("github", operation_id="op_1")
        self._write_receipt(
            "github", cleanup_status="not_applicable", cleanup_confirmed=True  # disagrees
        )
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_invalid_cleanup_status_enum_fails(self):
        self._write_run("github", operation_id="op_1")
        self._write_receipt("github", cleanup_status="bogus_value")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_readback_result_must_be_null_for_read_effect(self):
        self._write_run("github", operation_id="op_1")
        self._write_receipt(
            "github",
            readback_result={
                "checked_at": "2026-01-01T00:00:00Z",
                "method": "independent_read",
                "matched": True,
                "detail": "x",
            },
        )
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            effect="read",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_readback_result_missing_subfields_for_write_effect_fails(self):
        self._write_run("github", operation_id="op_1")
        self._write_receipt(
            "github", readback_result={"checked_at": "2026-01-01T00:00:00Z"}  # incomplete
        )
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            effect="write",
            retry={"idempotency_class": "none_verify_by_readback", "detail": "x"},
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_well_formed_readback_result_for_write_effect_passes(self):
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt(
            "github",
            readback_result={
                "checked_at": "2026-01-01T00:00:00Z",
                "method": "independent_read",
                "matched": True,
                "detail": "x",
            },
        )
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            effect="write",
            retry={"idempotency_class": "none_verify_by_readback", "detail": "x"},
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "code_complete",
                    "last_reached_status": "code_complete",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert result.ok, result.findings

    def _write_effect_entry(self, **overrides):
        base = dict(
            service_id="github",
            operation_id="op_1",
            effect="write",
            retry={"idempotency_class": "none_verify_by_readback", "detail": "x"},
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "code_complete",
                    "last_reached_status": "code_complete",
                }
            ],
        )
        base.update(overrides)
        return _entry(**base)

    def test_receipt_null_readback_on_write_effect_fails(self):
        """A null readback_result on a write-shaped effect must be
        rejected: a receipt claiming runtime_verified=true with no
        readback at all, and cleanup_status=pending, would otherwise
        certify an unverified, uncleaned mutation with no structural
        objection."""
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt(
            "github", readback_result=None, cleanup_status="pending", cleanup_confirmed=False
        )
        entry = self._write_effect_entry()
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_readback_matched_false_on_write_effect_fails(self):
        """A readback that itself reports matched=false did NOT confirm
        the mutation took place as claimed — such a receipt cannot
        certify the verification_contract."""
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt(
            "github",
            readback_result={
                "checked_at": "2026-01-01T00:00:00Z",
                "method": "independent_read",
                "matched": False,
                "detail": "did not match",
            },
        )
        entry = self._write_effect_entry()
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_not_automatable_cleanup_on_write_effect_fails(self):
        """write always has a revert path per the spec's own per-effect
        table (delete or revert, confirmed once independently read back)
        — not_automatable is illegal cleanup ground for it."""
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt(
            "github",
            readback_result={
                "checked_at": "2026-01-01T00:00:00Z",
                "method": "independent_read",
                "matched": True,
                "detail": "x",
            },
            cleanup_status="not_automatable",
            cleanup_confirmed=False,
        )
        entry = self._write_effect_entry()
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_receipt_not_automatable_cleanup_on_external_send_effect_passes(self):
        """external_send legitimately allows not_automatable per the
        spec's own per-effect table when the vendor exposes no
        recall/delete-sent-item API."""
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt(
            "github",
            readback_result={
                "checked_at": "2026-01-01T00:00:00Z",
                "method": "vendor_delivery_id",
                "matched": True,
                "detail": "x",
            },
            cleanup_status="not_automatable",
            cleanup_confirmed=False,
        )
        entry = self._write_effect_entry(
            effect="external_send",
            retry={"idempotency_class": "none_verify_by_readback", "detail": "x"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert result.ok, result.findings

    def test_unhashable_effect_in_receipt_shape_path_never_crashes(self):
        """_check_receipt_shape's cleanup-status rule used a raw
        `effect in _NO_NOT_AUTOMATABLE_EFFECTS` membership test — an
        unhashable JSON effect (`[]`/`{}`) reaching this path after
        evidence resolves raised an uncaught TypeError, aborting the scan
        and discarding every diagnostic accumulated so far, instead of the
        one malformed entry failing cleanly."""
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt(
            "github",
            readback_result={
                "checked_at": "2026-01-01T00:00:00Z",
                "method": "independent_read",
                "matched": True,
                "detail": "x",
            },
        )
        entry = self._write_effect_entry(effect=[])
        result = gate.validate_entry(  # must not raise
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok

    def test_run_missing_required_string_field_fails(self):
        """The loader (_load_jsonl) only checks a run record is a JSON
        object with a unique string run_id — it never validated the OTHER
        required fields a ConformanceRun must carry. A run record missing
        tested_sha silently passed the staleness guard elsewhere in this
        file (which treats "field absent on the run" as "nothing to
        compare"), so the entry certified against an incomplete run with
        no structural objection."""
        self._write_run("github", operation_id="op_1", tested_sha=None)
        self._write_receipt("github")
        entry = self._write_effect_entry(effect="read", retry=None)
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any(f.field.endswith(".tested_sha") for f in result.findings), result.findings

    def test_run_invalid_verdict_enum_fails(self):
        self._write_run("github", operation_id="op_1", verdict="bogus")
        self._write_receipt("github")
        entry = self._write_effect_entry(effect="read", retry=None)
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any(f.field.endswith(".verdict") for f in result.findings), result.findings

    def test_run_malformed_response_summary_fails(self):
        self._write_run(
            "github",
            operation_id="op_1",
            response_summary={"fields_present": "not_a_list", "types_matched": "not_a_bool"},
        )
        self._write_receipt("github")
        entry = self._write_effect_entry(effect="read", retry=None)
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any("response_summary" in f.field for f in result.findings), result.findings

    def test_matrix_cell_promotion_requires_receipt_runtime_verified(self):
        """The entry-level check at contract_verified-or-later resolves the
        run's own receipt and requires runtime_verified: true, but the
        per-cell check in the evidence matrix only checked
        run.verdict == "pass" — it never resolved the cell's own run's
        receipt or checked runtime_verified at all. A cell could promote to
        contract_verified on a passing run whose receipt was never actually
        runtime-verified. Asserts the SPECIFIC cell-level field, not just
        any failure, so this test cannot pass merely because the
        independent entry-level check (which covers different ground) also
        objects to the same fixture."""
        self._write_run("github", operation_id="op_1")
        self._write_receipt("github", runtime_verified=False)
        entry = self._write_effect_entry(
            effect="read",
            retry=None,
            status="contract_verified",
            last_reached_status="contract_verified",
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any(
            f.field == "verification_contract_ref" and "runtime_verified" in f.message
            for f in result.findings
        ), result.findings


class TestFieldTypeEnforcement:
    """Several documented array/string fields were
    checked for presence and, for arrays, container type — but never their
    OWN elements' or scalar's type. A string-valued array or a non-string
    scalar in a documented string field was silently accepted."""

    @pytest.mark.parametrize("field_name", ["operation_id", "provider", "observed_at"])
    def test_non_string_scalar_field_is_rejected(self, field_name):
        entry = _entry(**{field_name: 42})
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    @pytest.mark.parametrize(
        "field_name", ["tool_names", "auth_modes", "scopes", "account_types", "surfaces"]
    )
    def test_non_string_array_element_is_rejected(self, field_name):
        entry = _entry(**{field_name: ["valid_string", 42]})
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    @pytest.mark.parametrize(
        "field_name", ["tool_names", "auth_modes", "scopes", "account_types", "surfaces"]
    )
    def test_non_array_value_for_array_field_is_rejected(self, field_name):
        entry = _entry(**{field_name: "not_a_list"})
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_code_refs_non_string_element_is_rejected(self):
        entry = _entry(code_refs=["valid/path.py", 42])
        result = gate.validate_entry(entry, _REF)
        assert not result.ok

    def test_code_refs_empty_array_is_legal(self):
        entry = _entry(code_refs=[])
        result = gate.validate_entry(entry, _REF)
        assert result.ok, result.findings

    # --- F5: scalar/nested string types that must be type-checked (cb817a12) ---

    def test_non_string_runner_version_is_rejected(self):
        entry = _entry(runner_version=42)
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == "runner_version" for f in result.findings), result.findings

    def test_non_string_pagination_is_rejected(self):
        # pagination is required only for list/search; on a single_fetch it may
        # be omitted, but a NON-STRING value (an object) must still be rejected.
        entry = _entry(operation_kind="list", pagination={"style": "cursor"})
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == "pagination" for f in result.findings), result.findings

    @pytest.mark.parametrize("sub", ["module_ref", "version"])
    def test_non_string_adapter_subfield_is_rejected(self, sub):
        adapter = {"module_ref": "kiro_crew.x", "version": "0.1.0"}
        adapter[sub] = 7
        entry = _entry(adapter=adapter)
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == f"adapter.{sub}" for f in result.findings), result.findings

    @pytest.mark.parametrize("schema_field", ["input_schema", "output_schema"])
    def test_non_string_schema_version_is_rejected(self, schema_field):
        schema = {"schema_ref": "https://example.com/s", "schema_version": 3}
        entry = _entry(**{schema_field: schema})
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(
            f.field == f"{schema_field}.schema_version" for f in result.findings
        ), result.findings

    # --- F4: entry path must match operation_id/service_id EXACTLY, not by
    # suffix — a rogue nested path must not be accepted (cb817a12). ---

    def test_rogue_nested_entry_path_is_rejected(self):
        # A file at entries/rogue/entries/github/svc_do_thing.json ends with the
        # canonical suffix but is NOT the canonical path; a suffix match would
        # have wrongly accepted it, letting two files claim one identity.
        rogue_ref = (
            "docs/system-specs/connector-manifest/entries/rogue/entries/" "github/svc_do_thing.json"
        )
        result = gate.validate_entry(_entry(), rogue_ref)
        assert not result.ok
        assert any(f.field == "operation_id/service_id" for f in result.findings), result.findings

    def test_canonical_entry_path_still_passes(self):
        # Guard against over-tightening: the real canonical path must pass.
        result = gate.validate_entry(_entry(), _REF)
        assert result.ok, result.findings


# ---------------------------------------------------------------------------
# Fixture entries this PR actually ships: prove they stay valid, not just
# that the validator's own synthetic inputs do.
# ---------------------------------------------------------------------------


class TestFixtureEntries:
    def test_all_shipped_fixture_entries_validate_clean(self):
        result = gate.run_scan()
        assert result.ok, result.findings

    def test_fixture_count_matches_expectation(self):
        paths = gate._iter_entry_files()
        # This repo ships exactly 1 fixture entry as of this PR (gh_search_repositories,
        # status: planned). A prior draft also shipped a synthetic contract_verified
        # fixture (gh_get_issue) with a fabricated ConformanceRun/EvidenceReceipt pair
        # at this same authoritative path — review correctly flagged this as fabricated
        # evidence in the authoritative record the spec's own text says is "written
        # once and never edited" / "kept, never deleted". That lifecycle case is
        # exercised instead by TestCrossFileConsistency above, against synthetic
        # dicts and temp-file jsonl fixtures that never land in the real, published
        # entries/runs/receipts trees. A future entry-population round will add many
        # more real entries; this assertion exists to catch an accidental fixture
        # deletion, not to cap growth — update the expected count in the SAME commit
        # that adds or removes a fixture.
        # assertion exists to catch an accidental fixture deletion, not to cap growth —
        # update the expected count in the SAME commit that adds or removes a fixture.
        assert len(paths) == 1, paths


# ---------------------------------------------------------------------------
# Subprocess-level: the gate's own --test and default scan modes, run exactly
# as CI will invoke them.
# ---------------------------------------------------------------------------


class TestCLI:
    def test_self_test_subcommand_passes(self):
        proc = subprocess.run(
            [sys.executable, _SCRIPT_PATH, "--test"],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_default_scan_passes_on_real_repo_state(self):
        proc = subprocess.run(
            [sys.executable, _SCRIPT_PATH],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_scan_fails_closed_on_a_broken_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "broken.json")
            with open(bad_path, "w", encoding="utf-8") as fh:
                json.dump({"operation_id": "incomplete"}, fh)
            proc = subprocess.run(
                [sys.executable, _SCRIPT_PATH, "--entry", bad_path],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
            )
            assert proc.returncode == 1
            assert "required field is missing" in (proc.stdout + proc.stderr)

    def test_invalid_json_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "broken.json")
            with open(bad_path, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            proc = subprocess.run(
                [sys.executable, _SCRIPT_PATH, "--entry", bad_path],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
            )
            assert proc.returncode == 1
            assert "invalid JSON" in (proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# F1: every committed runs/*.jsonl and receipts/*.jsonl record is validated
# INDEPENDENTLY of manifest references (cb817a12). Orphan/malformed evidence
# must not ship uninspected.
# ---------------------------------------------------------------------------


class TestOrphanEvidenceScan:
    @pytest.fixture(autouse=True)
    def _isolated_roots(self, tmp_path, monkeypatch):
        entries_dir = tmp_path / "entries"
        runs_dir = tmp_path / "runs"
        receipts_dir = tmp_path / "receipts"
        entries_dir.mkdir()
        runs_dir.mkdir()
        receipts_dir.mkdir()
        monkeypatch.setattr(gate, "ENTRIES_ROOT", str(entries_dir))
        monkeypatch.setattr(gate, "RUNS_ROOT", str(runs_dir))
        monkeypatch.setattr(gate, "RECEIPTS_ROOT", str(receipts_dir))
        self.runs_dir = runs_dir
        self.receipts_dir = receipts_dir

    def _write_line(self, path, record):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def test_clean_when_no_evidence_and_no_entries(self):
        # No entries, no evidence -> nothing to validate, scan is clean.
        result = gate.run_scan()
        assert result.ok, result.findings

    def test_orphan_run_missing_required_fields_is_rejected(self):
        # A run record referenced by NO entry, missing required fields, must
        # still be flagged by the independent orphan-evidence scan.
        self._write_line(self.runs_dir / "github.jsonl", {"run_id": "orphan_run"})
        result = gate.run_scan()
        assert not result.ok
        assert any(
            "orphan_run" in f.field or "orphan_run" in f.message for f in result.findings
        ), result.findings

    def test_orphan_receipt_missing_required_fields_is_rejected(self):
        # A receipt missing claim/negative_test_refs, referenced by nothing.
        self._write_line(
            self.receipts_dir / "github.jsonl",
            {"receipt_id": "orphan_receipt", "conformance_run_ref": "x"},
        )
        result = gate.run_scan()
        assert not result.ok

    def test_valid_read_orphan_receipt_with_null_readback_passes(self):
        # F1 regression guard: an orphan receipt from a READ operation legally
        # carries readback_result: null. The orphan scan does not know the
        # effect, so it must NOT apply the "every effect except read requires
        # readback" rule and falsely reject this structurally-valid receipt.
        # A matching run is written so the F4 back-pointer resolves (this test
        # is about the readback rule, not a dangling reference).
        self._write_line(
            self.runs_dir / "github.jsonl",
            {
                "run_id": "run_x",
                "operation_id": "op_1",
                "account_binding_ref": "fixture",
                "auth_mode": "oauth_user",
                "account_type": "personal",
                "surface": "chat",
                "tested_sha": "a" * 40,
                "adapter_version": "0.1.0",
                "input_schema_version": "1",
                "output_schema_version": "1",
                "runner_version": "0.1.0",
                "executed_at": "2026-01-01T00:00:00Z",
                "request_shape_hash": "sha256:0",
                "response_summary": {
                    "fields_present": [],
                    "types_matched": True,
                    "unexpected_fields": [],
                },
                "verdict": "pass",
                "evidence_receipt_ref": "r_read",
            },
        )
        self._write_line(
            self.receipts_dir / "github.jsonl",
            {
                "receipt_id": "r_read",
                "conformance_run_ref": "run_x",
                "claim": "read op verified",
                "runtime_verified": True,
                "readback_result": None,
                "cleanup_confirmed": False,
                "cleanup_status": "not_applicable",
                "negative_test_refs": ["neg_1"],
            },
        )
        result = gate.run_scan()
        assert result.ok, result.findings

    def test_orphan_receipt_blank_receipt_id_is_rejected(self):
        # Exercises the receipt_id shape check via the orphan path — the
        # entry-level path can't (an unresolvable ref fires first).
        self._write_line(
            self.receipts_dir / "github.jsonl",
            {
                "receipt_id": "",
                "conformance_run_ref": "run_x",
                "claim": "c",
                "runtime_verified": True,
                "readback_result": None,
                "cleanup_confirmed": False,
                "cleanup_status": "not_applicable",
                "negative_test_refs": ["neg_1"],
            },
        )
        result = gate.run_scan()
        assert not result.ok
        assert any("receipt_id" in f.field for f in result.findings), result.findings

    def test_duplicate_run_id_in_orphan_log_is_rejected(self):
        self._write_line(self.runs_dir / "github.jsonl", {"run_id": "dup"})
        self._write_line(self.runs_dir / "github.jsonl", {"run_id": "dup"})
        result = gate.run_scan()
        assert not result.ok
        assert any("duplicate" in f.message for f in result.findings), result.findings

    def test_non_object_line_in_orphan_log_is_rejected(self):
        with open(self.runs_dir / "github.jsonl", "a", encoding="utf-8") as fh:
            fh.write("42\n")
        result = gate.run_scan()
        assert not result.ok

    def test_single_entry_check_skips_orphan_evidence_scan(self):
        # A targeted --entry check (entry_paths given) must NOT fail on an
        # unrelated malformed evidence log elsewhere in the tree.
        self._write_line(self.runs_dir / "github.jsonl", {"run_id": "orphan_run"})
        # scan only a (nonexistent-but-irrelevant) path list: the orphan run
        # must be ignored because entry_paths is not None.
        result = gate.run_scan(entry_paths=[])
        assert result.ok, result.findings


# ---------------------------------------------------------------------------
# F3: EvidenceReceipt required-field presence/type (cb817a12). Exercised at
# the receipt-shape level via a code_complete entry's verification_contract.
# ---------------------------------------------------------------------------


class TestReceiptRequiredFields(TestCrossFileConsistency):
    """Reuses TestCrossFileConsistency's isolated RUNS/RECEIPTS roots and
    _write_run/_write_receipt helpers."""

    def _code_complete_entry(self):
        return _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )

    @pytest.mark.parametrize("missing", ["conformance_run_ref", "claim"])
    def test_receipt_missing_required_string_field_is_rejected(self, missing):
        # NB: receipt_id is NOT parametrized here — a receipt with a null
        # receipt_id cannot be resolved by _find_by_id at the entry level, so
        # the unresolved-reference finding fires BEFORE _check_receipt_shape
        # and the test would pass for an unrelated reason. receipt_id's own
        # shape check is instead exercised directly by the orphan-evidence
        # scan test (TestOrphanEvidenceScan), where the record is enumerated
        # rather than resolved through a reference.
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt("github", **{missing: None})
        result = gate.validate_entry(
            self._code_complete_entry(),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert not result.ok
        assert any(
            missing in f.field and "receipt_ref" in f.field for f in result.findings
        ), result.findings

    def test_receipt_non_boolean_runtime_verified_is_rejected(self):
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt("github", runtime_verified="yes")
        result = gate.validate_entry(
            self._code_complete_entry(),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert not result.ok

    def test_receipt_empty_negative_test_refs_is_rejected(self):
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt("github", negative_test_refs=[])
        result = gate.validate_entry(
            self._code_complete_entry(),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert not result.ok


# ---------------------------------------------------------------------------
# F2: a matrix CELL reference is validated to the SAME depth as the
# entry-level verification_contract (cb817a12) — full run/receipt shape and
# the receipt->run back-pointer, not just coordinates + runtime_verified.
# ---------------------------------------------------------------------------


class TestPerCellFullValidation(TestCrossFileConsistency):
    def _entry_with_cell(self, cell_run_ref="run_1"):
        # The entry-level verification_contract points at a VALID run/receipt
        # (run_good/receipt_good) so the entry-level check passes cleanly; the
        # matrix CELL points at cell_run_ref, which the tests make malformed.
        # This isolates the per-cell check from the entry-level one, so a
        # failure can only come from the cell path (F2), not incidentally from
        # the entry-level validation of the same run.
        return _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            verification_contract={"run_ref": "run_good", "receipt_ref": "receipt_good"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": cell_run_ref,
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )

    def _write_good_entry_level_evidence(self):
        # A fully-valid run/receipt pair the entry-level contract points at.
        self._write_run(
            "github",
            run_id="run_good",
            operation_id="op_1",
            input_schema_version="1",
            output_schema_version="1",
            adapter_version="0.1.0",
            runner_version="0.1.0",
            evidence_receipt_ref="receipt_good",
        )
        self._write_receipt("github", receipt_id="receipt_good", conformance_run_ref="run_good")

    def test_cell_run_missing_required_field_is_rejected(self):
        # The CELL run resolves and coordinates match, but the run is missing a
        # required field (blank runner_version). The old per-cell check only
        # looked at verdict + runtime_verified and would have missed this.
        self._write_good_entry_level_evidence()
        self._write_run(
            "github",
            run_id="run_1",
            operation_id="op_1",
            input_schema_version="1",
            output_schema_version="1",
            runner_version="",  # blank -> per-cell _validate_run must flag it
            evidence_receipt_ref="receipt_1",
        )
        self._write_receipt("github", receipt_id="receipt_1", conformance_run_ref="run_1")
        result = gate.validate_entry(
            self._entry_with_cell(cell_run_ref="run_1"),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert not result.ok
        assert any(
            "runner_version" in f.field and "evidence_by_mode_surface_and_auth[0]" in f.entry_ref
            for f in result.findings
        ), result.findings

    def test_cell_receipt_wrong_back_pointer_is_rejected(self):
        # CELL receipt resolves and runtime_verified is true, but its
        # conformance_run_ref points at a DIFFERENT run — the old per-cell
        # check never verified the receipt->run back-pointer.
        self._write_good_entry_level_evidence()
        self._write_run(
            "github",
            run_id="run_1",
            operation_id="op_1",
            input_schema_version="1",
            output_schema_version="1",
            evidence_receipt_ref="receipt_1",
        )
        self._write_receipt("github", receipt_id="receipt_1", conformance_run_ref="some_other_run")
        result = gate.validate_entry(
            self._entry_with_cell(cell_run_ref="run_1"),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert not result.ok
        assert any(
            "certifies a different run" in f.message for f in result.findings
        ), result.findings

    def test_cell_run_stale_immutable_ref_is_rejected(self):
        # :1543 — the cell run resolves and coordinates match, but its bound
        # runner_version drifts from the entry's; the per-cell check must apply
        # the same five-field immutable-ref staleness rule the entry level does.
        self._write_good_entry_level_evidence()
        self._write_run(
            "github",
            run_id="run_1",
            operation_id="op_1",
            input_schema_version="1",
            output_schema_version="1",
            adapter_version="0.1.0",
            runner_version="9.9.9-STALE",  # != entry's runner_version 0.1.0
            evidence_receipt_ref="receipt_1",
        )
        self._write_receipt("github", receipt_id="receipt_1", conformance_run_ref="run_1")
        result = gate.validate_entry(
            self._entry_with_cell(cell_run_ref="run_1"),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert not result.ok
        assert any(
            f.field == "runner_version"
            and "evidence_by_mode_surface_and_auth[0]" in f.entry_ref
            and "STALE" in f.message
            for f in result.findings
        ), result.findings

    def test_same_run_stale_reported_once_not_duplicated(self):
        # LOW (round-16): when the entry-level verification_contract AND the
        # matrix cell cite the SAME run, a drifted bound field must be reported
        # ONCE (at the entry ref), not duplicated at the row ref.
        self._write_run(
            "github",
            run_id="run_1",
            operation_id="op_1",
            input_schema_version="1",
            output_schema_version="1",
            adapter_version="0.1.0",
            runner_version="9.9.9-STALE",  # drifts from entry's 0.1.0
            evidence_receipt_ref="receipt_1",
        )
        self._write_receipt("github", receipt_id="receipt_1", conformance_run_ref="run_1")
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            # entry-level AND cell both cite run_1 (the same run)
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": "run_1",
                    "status": "contract_verified",
                    "last_reached_status": "contract_verified",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        runner_stale = [
            f for f in result.findings if f.field == "runner_version" and "STALE" in f.message
        ]
        assert len(runner_stale) == 1, runner_stale


# ---------------------------------------------------------------------------
# The authoritative SCOPE_CATALOG must be statically readable, or membership
# validation is silently toothless. Each way it can become unreadable must
# fail the whole-tree scan LOUDLY with a readable reason — never a silent skip
# and never a masked "everything rejected". Same shape as the "delete the
# catalog file to turn off the 273/72 invariant" failure mode.
# ---------------------------------------------------------------------------


class TestScopeCatalogFailClosed:
    @pytest.fixture(autouse=True)
    def _reset_catalog_cache(self):
        # The loader caches globally; reset before and after each case so a
        # temp governance.py is actually re-read.
        def _reset():
            gate._SCOPE_CATALOG_CACHE = None
            gate._SCOPE_CATALOG_ERROR = None
            gate._SCOPE_CATALOG_LOADED = False

        _reset()
        yield
        _reset()

    def _point_governance_at(self, monkeypatch, source_or_none, tmp_path):
        if source_or_none is None:
            # Case 1: a path that does not exist.
            monkeypatch.setattr(gate, "GOVERNANCE_PY", str(tmp_path / "does_not_exist.py"))
        else:
            p = tmp_path / "governance.py"
            p.write_text(source_or_none, encoding="utf-8")
            monkeypatch.setattr(gate, "GOVERNANCE_PY", str(p))

    def test_case1_missing_file_fails_closed(self, monkeypatch, tmp_path):
        self._point_governance_at(monkeypatch, None, tmp_path)
        assert gate._governance_scope_catalog() is None
        assert gate._SCOPE_CATALOG_ERROR and "could not be read" in gate._SCOPE_CATALOG_ERROR
        result = gate.ValidationResult(ok=True)
        gate._check_scope_catalog_readable(result)
        assert not result.ok
        assert any(f.field == "governance.SCOPE_CATALOG" for f in result.findings)

    def test_case2_no_assignment_node_fails_closed(self, monkeypatch, tmp_path):
        self._point_governance_at(monkeypatch, "X = 1\nY = {'a': 2}\n", tmp_path)
        assert gate._governance_scope_catalog() is None
        assert (
            gate._SCOPE_CATALOG_ERROR and "no SCOPE_CATALOG assignment" in gate._SCOPE_CATALOG_ERROR
        )
        result = gate.ValidationResult(ok=True)
        gate._check_scope_catalog_readable(result)
        assert not result.ok

    def test_case3_not_a_static_literal_fails_closed(self, monkeypatch, tmp_path):
        # A comprehension instead of a dict literal — cannot be read statically.
        self._point_governance_at(
            monkeypatch, "SCOPE_CATALOG = {k: 1 for k in ['tools', 'mcp']}\n", tmp_path
        )
        assert gate._governance_scope_catalog() is None
        assert (
            gate._SCOPE_CATALOG_ERROR
            and "no longer a static dict literal" in gate._SCOPE_CATALOG_ERROR
        )
        result = gate.ValidationResult(ok=True)
        gate._check_scope_catalog_readable(result)
        assert not result.ok

    def test_case3b_non_literal_key_fails_closed(self, monkeypatch, tmp_path):
        self._point_governance_at(
            monkeypatch, "BASE = {'x': 1}\nSCOPE_CATALOG = {'tools': 1, **BASE}\n", tmp_path
        )
        assert gate._governance_scope_catalog() is None
        assert gate._SCOPE_CATALOG_ERROR and "non-string-literal key" in gate._SCOPE_CATALOG_ERROR

    def test_empty_catalog_fails_closed(self, monkeypatch, tmp_path):
        self._point_governance_at(monkeypatch, "SCOPE_CATALOG = {}\n", tmp_path)
        assert gate._governance_scope_catalog() is None
        assert gate._SCOPE_CATALOG_ERROR and "EMPTY key set" in gate._SCOPE_CATALOG_ERROR

    def test_readable_literal_is_accepted(self, monkeypatch, tmp_path):
        # Baseline check: a well-formed literal is read, and does NOT trip the gate.
        self._point_governance_at(
            monkeypatch,
            "SCOPE_CATALOG = {\n    'tools': 1,\n    'filesystem.read': 2,\n}\n",
            tmp_path,
        )
        catalog = gate._governance_scope_catalog()
        assert catalog == frozenset({"tools", "filesystem.read"})
        result = gate.ValidationResult(ok=True)
        gate._check_scope_catalog_readable(result)
        assert result.ok, result.findings

    def test_unreadable_catalog_fails_scan_even_with_all_null_policies(self, monkeypatch, tmp_path):
        # The core anti-silent-skip guarantee, exercised END TO END through
        # run_scan() (NOT the helper directly) so it also guards the run_scan
        # wiring: if the _check_scope_catalog_readable call were removed from
        # run_scan, or moved off the whole-tree branch, this test fails.
        # run_scan() scans the real shipped fixture, whose policy scopes are all
        # null — precisely the case where the OLD code silently skipped.
        # Baseline: real catalog readable -> whole-tree scan is clean.
        gate._SCOPE_CATALOG_CACHE = None
        gate._SCOPE_CATALOG_ERROR = None
        gate._SCOPE_CATALOG_LOADED = False
        baseline = gate.run_scan()
        assert baseline.ok, baseline.findings
        # Poison: point the catalog at a missing file, reset the cache, re-scan.
        self._point_governance_at(monkeypatch, None, tmp_path)
        gate._SCOPE_CATALOG_CACHE = None
        gate._SCOPE_CATALOG_ERROR = None
        gate._SCOPE_CATALOG_LOADED = False
        poisoned = gate.run_scan()
        assert not poisoned.ok, "an unreadable catalog must fail the WHOLE-TREE scan"
        assert any(
            f.field == "governance.SCOPE_CATALOG" for f in poisoned.findings
        ), poisoned.findings

    def test_single_entry_check_does_not_run_catalog_gate(self, monkeypatch, tmp_path):
        # The whole-tree catalog gate must NOT fire for a targeted --entry
        # check (entry_paths given), same isolation as the orphan scan: a
        # single-entry validation must not fail on an unreadable catalog it
        # was not asked to check. Use the real fixture entry as the single path.
        self._point_governance_at(monkeypatch, None, tmp_path)
        gate._SCOPE_CATALOG_CACHE = None
        gate._SCOPE_CATALOG_ERROR = None
        gate._SCOPE_CATALOG_LOADED = False
        entry_path = os.path.join(gate.ENTRIES_ROOT, "github", "gh_search_repositories.json")
        result = gate.run_scan(entry_paths=[entry_path])
        assert not any(
            f.field == "governance.SCOPE_CATALOG" for f in result.findings
        ), "single --entry must not run the whole-tree catalog gate"


# ---------------------------------------------------------------------------
# GPT 5.6 findings on head 691f5e931: each rule below is exercised by a
# negative test that fails for the specific rule, not an unrelated one.
# ---------------------------------------------------------------------------


class TestGpt691Findings:
    def test_missing_entry_path_fails_closed_not_uncaught(self):
        # :1833 — a --entry path that does not exist must produce a <file>
        # finding, not raise an uncaught OSError out of run_scan.
        result = gate.run_scan(entry_paths=["/no/such/entry/file.json"])
        assert not result.ok
        assert any(
            f.field == "<file>" and "could not be read" in f.message for f in result.findings
        ), result.findings

    def test_hostless_https_url_is_rejected(self):
        # :211 — a URL with a scheme but no host (netloc "user@" / ":443") is
        # not resolvable; parsed.hostname is None there.
        assert gate._is_resolvable_https_url("https://user@") is False
        assert gate._is_resolvable_https_url("https://:443/x") is False
        assert gate._is_resolvable_https_url("https://") is False
        assert gate._is_resolvable_https_url("https://docs.github.com/x") is True

    def test_source_scalar_types_enforced(self):
        # :211 — source_id non-string, and a non-null-source_kind with null
        # observed_at, are rejected.
        e1 = _entry(
            source={
                "source_kind": "official_docs",
                "source_id": 42,
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "https://x.example.com/y",
            },
            source_status="official_baseline",
        )
        r1 = gate.validate_entry(e1, _REF)
        assert not r1.ok and any(f.field == "source.source_id" for f in r1.findings), r1.findings

        e2 = _entry(
            source={
                "source_kind": "official_docs",
                "source_id": "ok",
                "observed_at": None,
                "snapshot_ref": "https://x.example.com/y",
            },
            source_status="official_baseline",
        )
        r2 = gate.validate_entry(e2, _REF)
        assert not r2.ok and any(f.field == "source.observed_at" for f in r2.findings), r2.findings

    def test_sha_populated_before_its_rung_is_rejected(self):
        # :671 — a planned entry may not carry a tested_sha/merged_sha/release_sha.
        for sha_field in ("tested_sha", "merged_sha", "release_sha"):
            entry = _entry(**{sha_field: "a" * 40})  # default status is planned
            result = gate.validate_entry(entry, _REF)
            assert not result.ok, sha_field
            assert any(
                f.field == sha_field and "must be null until status reaches" in f.message
                for f in result.findings
            ), (sha_field, result.findings)

    def test_valid_planned_entry_has_all_shas_null(self):
        # Guard against over-tightening: the shipped planned fixture shape passes.
        result = gate.validate_entry(_entry(), _REF)
        assert result.ok, result.findings


class TestGpt691MatrixFindings(TestCrossFileConsistency):
    def _matrix_entry(self, rows):
        return _entry(
            service_id="github",
            operation_id="op_1",
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            status="implementing",
            last_reached_status="implementing",
            evidence_by_mode_surface_and_auth=rows,
        )

    def test_undeclared_coordinate_row_is_rejected(self):
        # :1405 — a row whose (auth_mode, account_type, surface) is not in the
        # declared axes product is rejected as an undeclared coordinate.
        rows = [
            {
                "auth_mode": "oauth_user",
                "account_type": "personal",
                "surface": "chat",
                "applicable": True,
                "exclusion_reason": None,
                "verification_contract_ref": None,
                "status": "implementing",
                "last_reached_status": "implementing",
            },
            {
                "auth_mode": "service_to_service",  # NOT in declared auth_modes
                "account_type": "personal",
                "surface": "chat",
                "applicable": True,
                "exclusion_reason": None,
                "verification_contract_ref": None,
                "status": "implementing",
                "last_reached_status": "implementing",
            },
        ]
        result = gate.validate_entry(
            self._matrix_entry(rows),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert not result.ok
        assert any("UNDECLARED" in f.message for f in result.findings), result.findings

    def test_excluded_row_with_verification_contract_ref_is_rejected(self):
        # :1405 — an applicable=false row must not also cite a vc_ref.
        rows = [
            {
                "auth_mode": "oauth_user",
                "account_type": "personal",
                "surface": "chat",
                "applicable": False,
                "exclusion_reason": "not supported on this surface",
                "verification_contract_ref": "run_x",
                "status": "planned",
                "last_reached_status": "planned",
            }
        ]
        result = gate.validate_entry(
            self._matrix_entry(rows),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert not result.ok
        assert any(
            f.field == "verification_contract_ref" and "applicable=false" in f.message
            for f in result.findings
        ), result.findings

    # HIGH (round-7): a status=planned matrix waives TOTALITY only — any rows
    # present must still obey row-level structure. Earlier the validator
    # early-returned on planned, so a planned entry with malformed rows bypassed
    # both new checks. These use a planned top-level status specifically.
    def _planned_matrix_entry(self, rows):
        return _entry(
            service_id="github",
            operation_id="op_1",
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            status="planned",
            last_reached_status="planned",
            evidence_by_mode_surface_and_auth=rows,
        )

    def test_planned_undeclared_coordinate_row_is_rejected(self):
        rows = [
            {
                "auth_mode": "service_to_service",  # undeclared
                "account_type": "personal",
                "surface": "chat",
                "applicable": True,
                "exclusion_reason": None,
                "verification_contract_ref": None,
                "status": "planned",
                "last_reached_status": "planned",
            }
        ]
        result = gate.validate_entry(
            self._planned_matrix_entry(rows),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert not result.ok
        assert any("UNDECLARED" in f.message for f in result.findings), result.findings

    def test_planned_excluded_row_with_vcref_is_rejected(self):
        rows = [
            {
                "auth_mode": "oauth_user",
                "account_type": "personal",
                "surface": "chat",
                "applicable": False,
                "exclusion_reason": "x",
                "verification_contract_ref": "run_x",
                "status": "planned",
                "last_reached_status": "planned",
            }
        ]
        result = gate.validate_entry(
            self._planned_matrix_entry(rows),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert not result.ok
        assert any("applicable=false" in f.message for f in result.findings), result.findings

    def test_planned_empty_and_partial_valid_matrices_pass(self):
        # Empty planned matrix and a GENUINELY partial (1-of-2) planned matrix
        # both pass — totality is waived at planned. The partial case declares
        # a 2-cell product (two surfaces) and supplies only one row, so it is
        # really partial, not a filled 1x1x1 product.
        empty = gate.validate_entry(
            self._planned_matrix_entry([]),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert empty.ok, empty.findings
        partial_entry = _entry(
            service_id="github",
            operation_id="op_1",
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat", "workflow"],  # 2-cell product
            status="planned",
            last_reached_status="planned",
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",  # only 1 of the 2 declared cells
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": None,
                    "status": "planned",
                    "last_reached_status": "planned",
                }
            ],
        )
        partial = gate.validate_entry(
            partial_entry,
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert partial.ok, partial.findings

    def test_implementing_partial_matrix_still_fails_totality(self):
        # Guard the other side: the SAME 1-of-2 shape at status=implementing
        # (past planned) must fail the totality check — proving the waiver is
        # planned-only.
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat", "workflow"],
            status="implementing",
            last_reached_status="implementing",
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
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any("not TOTAL" in f.message for f in result.findings), result.findings


class TestGpt691ReceiptReadbackTypes(TestCrossFileConsistency):
    def _cc_entry(self):
        return _entry(
            service_id="github",
            operation_id="op_1",
            effect="write",
            retry={"idempotency_class": "external_id_upsert", "detail": "x"},
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )

    def test_non_string_readback_subfield_is_rejected(self):
        # :981 — a receipt whose readback checked_at/method/detail is non-string
        # must be rejected (they are `string` per the spec's EvidenceReceipt row).
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt(
            "github",
            cleanup_status="confirmed",
            cleanup_confirmed=True,
            readback_result={
                "checked_at": 12345,  # non-string
                "method": "independent_read",
                "matched": True,
                "detail": "read back ok",
            },
        )
        result = gate.validate_entry(
            self._cc_entry(),
            "docs/system-specs/connector-manifest/entries/github/op_1.json",
        )
        assert not result.ok
        assert any(
            "readback_result.checked_at" in f.field for f in result.findings
        ), result.findings


# ---------------------------------------------------------------------------
# GPT 5.6 findings on head 487f096652 — each fixed with a negative test that
# fails via the specific rule.
# ---------------------------------------------------------------------------


class TestGpt487Findings:
    def test_contradictory_source_kind_status_pairing_is_rejected(self):
        # F1 :631 — not_yet_sourced must pair with source_status unverified;
        # pairing it with official_baseline is a provenance contradiction.
        entry = _entry(
            source={
                "source_kind": "not_yet_sourced",
                "source_id": "placeholder",
                "observed_at": None,
                "snapshot_ref": "NOT_YET_SOURCED",
            },
            source_status="official_baseline",
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(
            f.field == "source_status" and "spec pairing" in f.message for f in result.findings
        ), result.findings

    def test_user_stated_requires_user_required(self):
        entry = _entry(
            source={
                "source_kind": "user_stated",
                "source_id": "brief-1",
                "observed_at": "2026-01-01T00:00:00Z",
                "snapshot_ref": "AGENTS.md",
            },
            source_status="unverified",
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(f.field == "source_status" for f in result.findings), result.findings

    def test_sha_field_with_movable_alias_is_rejected(self):
        # F5 :824 — a movable alias like "main" must not fill a commit-SHA field.
        entry = _entry(
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="main",  # movable alias, not a hex object name
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            verification_contract={"run_ref": "r", "receipt_ref": "rc"},
        )
        result = gate.validate_entry(entry, _REF)
        assert not result.ok
        assert any(
            f.field == "tested_sha" and "immutable commit SHA" in f.message for f in result.findings
        ), result.findings

    def test_hex_sha_is_accepted_shape(self):
        assert gate._is_commit_sha("a" * 40) is True
        assert gate._is_commit_sha("0123abcd") is True
        assert gate._is_commit_sha("main") is False
        assert gate._is_commit_sha("v1.2.3") is False
        assert gate._is_commit_sha("HEAD") is False


class TestGpt487ReadReceiptCleanup(TestCrossFileConsistency):
    def test_read_receipt_with_non_not_applicable_cleanup_is_rejected(self):
        # F3 :1044 — a read effect's receipt must have cleanup_status
        # not_applicable; pending must be rejected even though readback is null.
        self._write_run(
            "github", operation_id="op_1", input_schema_version="1", output_schema_version="1"
        )
        self._write_receipt(
            "github", readback_result=None, cleanup_status="pending", cleanup_confirmed=False
        )
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            effect="read",
            status="code_complete",
            last_reached_status="code_complete",
            tested_sha="a" * 40,
            runner_version="0.1.0",
            adapter={"module_ref": "kiro_crew.x", "version": "0.1.0"},
            verification_contract={"run_ref": "run_1", "receipt_ref": "receipt_1"},
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any(
            "cleanup_status" in f.field and "not_applicable" in f.message for f in result.findings
        ), result.findings


class TestGpt487MatrixRowLifecycle(TestCrossFileConsistency):
    def test_excluded_row_with_contradictory_lifecycle_is_rejected(self):
        # F2 :1501 — an excluded row whose status != last_reached_status (not
        # blocked) is malformed and must be caught even though applicable=false.
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            status="implementing",
            last_reached_status="implementing",
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": False,
                    "exclusion_reason": "policy denies",
                    "verification_contract_ref": None,
                    "status": "release_verified",  # contradicts last_reached
                    "last_reached_status": "planned",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        assert not result.ok
        assert any(f.field == "last_reached_status" for f in result.findings), result.findings


class TestGpt487OrphanBackPointer(TestOrphanEvidenceScan):
    def test_orphan_run_naming_nonexistent_receipt_is_rejected(self):
        # F4 :2006 — an orphan run whose evidence_receipt_ref resolves to no
        # receipt is a dangling reference and must fail the scan.
        self._write_line(
            self.runs_dir / "github.jsonl",
            {
                "run_id": "run_dangling",
                "operation_id": "op_1",
                "account_binding_ref": "x",
                "auth_mode": "oauth_user",
                "account_type": "personal",
                "surface": "chat",
                "tested_sha": "a" * 40,
                "adapter_version": "0.1.0",
                "input_schema_version": "1",
                "output_schema_version": "1",
                "runner_version": "0.1.0",
                "executed_at": "2026-01-01T00:00:00Z",
                "request_shape_hash": "sha256:0",
                "response_summary": {
                    "fields_present": [],
                    "types_matched": True,
                    "unexpected_fields": [],
                },
                "verdict": "pass",
                "evidence_receipt_ref": "receipt_does_not_exist",
            },
        )
        result = gate.run_scan()
        assert not result.ok
        assert any(
            "does not resolve to any EvidenceReceipt" in f.message for f in result.findings
        ), result.findings


# ---------------------------------------------------------------------------
# round-11 verifier findings (on 2afadcbb): reciprocity, SHA whitespace,
# duplicate status finding.
# ---------------------------------------------------------------------------


class TestRound11Fixes(TestOrphanEvidenceScan):
    def _write_run_full(self, run_id, evidence_receipt_ref):
        self._write_line(
            self.runs_dir / "github.jsonl",
            {
                "run_id": run_id,
                "operation_id": "op_1",
                "account_binding_ref": "x",
                "auth_mode": "oauth_user",
                "account_type": "personal",
                "surface": "chat",
                "tested_sha": "a" * 40,
                "adapter_version": "0.1.0",
                "input_schema_version": "1",
                "output_schema_version": "1",
                "runner_version": "0.1.0",
                "executed_at": "2026-01-01T00:00:00Z",
                "request_shape_hash": "sha256:0",
                "response_summary": {
                    "fields_present": [],
                    "types_matched": True,
                    "unexpected_fields": [],
                },
                "verdict": "pass",
                "evidence_receipt_ref": evidence_receipt_ref,
            },
        )

    def _write_receipt_full(self, receipt_id, conformance_run_ref):
        self._write_line(
            self.receipts_dir / "github.jsonl",
            {
                "receipt_id": receipt_id,
                "conformance_run_ref": conformance_run_ref,
                "claim": "c",
                "runtime_verified": True,
                "readback_result": None,
                "cleanup_confirmed": False,
                "cleanup_status": "not_applicable",
                "negative_test_refs": ["neg_1"],
            },
        )

    def test_second_receipt_pointing_at_paired_run_is_rejected(self):
        # MEDIUM: run<->receipt is one-to-one. run_1 pairs with receipt_1; a
        # second receipt_2 also naming run_1 breaks reciprocity.
        self._write_run_full("run_1", "receipt_1")
        self._write_receipt_full("receipt_1", "run_1")
        self._write_receipt_full("receipt_2", "run_1")  # extra, breaks 1:1
        result = gate.run_scan()
        assert not result.ok
        assert any("not one-to-one" in f.message for f in result.findings), result.findings

    def test_whitespace_padded_sha_is_rejected(self):
        # MEDIUM: a commit SHA is compared byte-for-byte; whitespace padding
        # makes it not a valid SHA.
        assert gate._is_commit_sha(" " + "a" * 40) is False
        assert gate._is_commit_sha("\t" + "a" * 7 + "\n") is False
        assert gate._is_commit_sha("a" * 40) is True


class TestRound11NoDuplicateStatusFinding:
    def test_invalid_row_status_reported_once(self):
        entry = _entry(
            service_id="github",
            operation_id="op_1",
            auth_modes=["oauth_user"],
            account_types=["personal"],
            surfaces=["chat"],
            status="implementing",
            last_reached_status="implementing",
            evidence_by_mode_surface_and_auth=[
                {
                    "auth_mode": "oauth_user",
                    "account_type": "personal",
                    "surface": "chat",
                    "applicable": True,
                    "exclusion_reason": None,
                    "verification_contract_ref": None,
                    "status": "bogus",  # invalid
                    "last_reached_status": "bogus",
                }
            ],
        )
        result = gate.validate_entry(
            entry, "docs/system-specs/connector-manifest/entries/github/op_1.json"
        )
        status_findings = [
            f for f in result.findings if f.field == "status" and "is not one of" in f.message
        ]
        assert len(status_findings) == 1, status_findings


# ---------------------------------------------------------------------------
# catalog-evidence.json count cross-check (FP item 8 + Design suggestion):
# the mirrored extract now has a real machine consumer, and the required-count
# denominators (72 / 273 / 247) are held machine-checked and fail-closed.
# ---------------------------------------------------------------------------


class TestCatalogEvidenceCounts:
    @pytest.fixture(autouse=True)
    def _isolate_catalog(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        entries = tmp_path / "entries"
        entries.mkdir()
        monkeypatch.setattr(gate, "ENTRIES_ROOT", str(entries))
        monkeypatch.setattr(gate, "RUNS_ROOT", str(tmp_path / "runs"))
        monkeypatch.setattr(gate, "RECEIPTS_ROOT", str(tmp_path / "receipts"))

    def _valid_catalog(self):
        fams = {
            "AUTH": 11,
            "GOV": 7,
            "RUN": 10,
            "KB": 10,
            "ACL": 9,
            "DATA": 7,
            "UX": 7,
            "SURF": 6,
            "OPS": 5,
        }  # sums to 72
        shared = {f: [{"contract_id": f"{f}-{i}"} for i in range(n)] for f, n in fams.items()}
        shared["microsoft_shared_auth_layer_note"] = {"note": "x"}
        return {
            "meta": {},
            "coverage": {
                "operations_by_evidence_tier": {
                    "source_verified_strict": 220,
                    "search_snippet_or_partial": 12,
                    "unverified": 40,
                },  # sums to 272
                "total_operations_all_tiers_combined": 272,
                "contract_attachment_operations": {"count": 1},
                "total_operations_all_tiers_combined_including_contract_attachment": 273,
                "required_acceptance_index_summary": {"total_entries": 247},
            },
            "shared_contracts": shared,
            "required_acceptance_index": [{"id": i} for i in range(247)],
        }

    def _point_at(self, obj_or_none):
        p = self.tmp / "catalog-evidence.json"
        if obj_or_none is not None:
            p.write_text(json.dumps(obj_or_none), encoding="utf-8")
        self.monkeypatch.setattr(gate, "CATALOG_EVIDENCE", str(p))

    def test_valid_catalog_passes(self):
        self._point_at(self._valid_catalog())
        result = gate.run_scan()
        assert result.ok, result.findings

    def test_missing_catalog_fails_closed(self):
        self.monkeypatch.setattr(gate, "CATALOG_EVIDENCE", str(self.tmp / "does_not_exist.json"))
        result = gate.run_scan()
        assert not result.ok
        assert any(f.field == "catalog-evidence.json" for f in result.findings), result.findings

    def test_shrunk_contract_count_is_rejected(self):
        cat = self._valid_catalog()
        cat["shared_contracts"]["OPS"] = cat["shared_contracts"]["OPS"][:-1]  # 72 -> 71
        self._point_at(cat)
        result = gate.run_scan()
        assert not result.ok
        assert any(f.field == "shared_contracts" for f in result.findings), result.findings

    def test_shrunk_acceptance_index_is_rejected(self):
        cat = self._valid_catalog()
        cat["required_acceptance_index"] = cat["required_acceptance_index"][:-1]  # 247 -> 246
        self._point_at(cat)
        result = gate.run_scan()
        assert not result.ok
        assert any(f.field == "required_acceptance_index" for f in result.findings), result.findings

    def test_broken_operations_reconciliation_is_rejected(self):
        cat = self._valid_catalog()
        cat["coverage"]["contract_attachment_operations"]["count"] = 2  # 272+2 != 273
        self._point_at(cat)
        result = gate.run_scan()
        assert not result.ok
        assert any(
            "reconciliation broken" in f.message or "must not shrink" in f.message
            for f in result.findings
        ), result.findings

    def test_missing_attachment_count_fails_closed(self):
        # HIGH regression: a missing contract_attachment_operations.count must
        # NOT skip the reconciliation and pass — it must fail closed.
        cat = self._valid_catalog()
        del cat["coverage"]["contract_attachment_operations"]["count"]
        self._point_at(cat)
        result = gate.run_scan()
        assert not result.ok
        assert any(
            f.field == "coverage.contract_attachment_operations.count" for f in result.findings
        ), result.findings

    def test_missing_attachment_object_fails_closed(self):
        cat = self._valid_catalog()
        del cat["coverage"]["contract_attachment_operations"]
        self._point_at(cat)
        result = gate.run_scan()
        assert not result.ok
        assert any(
            f.field == "coverage.contract_attachment_operations.count" for f in result.findings
        ), result.findings

    def test_missing_total_all_tiers_fails_closed(self):
        cat = self._valid_catalog()
        del cat["coverage"]["total_operations_all_tiers_combined"]
        self._point_at(cat)
        result = gate.run_scan()
        assert not result.ok
        assert any(
            f.field == "coverage.total_operations_all_tiers_combined" for f in result.findings
        ), result.findings

    def test_boolean_count_is_rejected(self):
        # HIGH regression: bool is a subclass of int in Python, but a JSON
        # boolean is not a JSON integer — `true` in a count must NOT satisfy
        # the reconciliation and fail open.
        cat = self._valid_catalog()
        cat["coverage"]["contract_attachment_operations"]["count"] = True
        self._point_at(cat)
        result = gate.run_scan()
        assert not result.ok
        assert any(
            f.field == "coverage.contract_attachment_operations.count" for f in result.findings
        ), result.findings

    def test_boolean_total_all_tiers_is_rejected(self):
        cat = self._valid_catalog()
        cat["coverage"]["total_operations_all_tiers_combined"] = True
        self._point_at(cat)
        result = gate.run_scan()
        assert not result.ok
        assert any(
            f.field == "coverage.total_operations_all_tiers_combined" for f in result.findings
        ), result.findings
