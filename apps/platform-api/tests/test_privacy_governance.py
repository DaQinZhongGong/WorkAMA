from workama_platform.modules.privacy.service import (
    build_export_manifest,
    deletion_steps,
    infer_processing_activity,
    transition_allowed,
)


def test_processing_activity_inference_is_conservative_for_sensitive_tables():
    assert infer_processing_activity("id_refresh_token").classification == "C4"
    assert infer_processing_activity("id_user").classification == "C3"
    assert infer_processing_activity("ag_session").classification == "C2"
    assert infer_processing_activity("unknown_future_table").classification == "C3"


def test_dsar_state_machine_rejects_skips_and_terminal_reentry():
    assert transition_allowed("requested", "identity_verification")
    assert transition_allowed("executing", "verification")
    assert transition_allowed("verification", "partially_completed")
    assert not transition_allowed("requested", "executing")
    assert not transition_allowed("completed", "executing")


def test_export_manifest_checksum_is_stable_and_changes_with_counts():
    first = build_export_manifest("dsr_1", "usr_1", {"sessions": 2, "artifacts": 1}, ["billing_ledger"])
    second = build_export_manifest("dsr_1", "usr_1", {"artifacts": 1, "sessions": 2}, ["billing_ledger"])
    changed = build_export_manifest("dsr_1", "usr_1", {"sessions": 3, "artifacts": 1}, ["billing_ledger"])
    assert first.checksum == second.checksum
    assert first.checksum != changed.checksum
    assert first.manifest["schema_version"] == "1"


def test_content_deletion_steps_cover_current_storage_surfaces():
    assert deletion_steps("content") == [
        "revoke_access",
        "delete_postgres_content",
        "delete_object_references",
        "purge_cache",
        "write_tombstone",
        "verify_absence",
    ]
