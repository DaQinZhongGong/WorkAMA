from workama_platform.modules.portability import canonical_package, validate_package


def test_workspace_package_validation_rejects_credentials_and_count_drift():
    package = {
        "manifest": {"manifest_version": 1, "resource_counts": {"channels": 1}},
        "resources": {"channels": [{"id": "chn_1", "credential_enc": "secret"}]},
    }
    errors = validate_package(package)
    assert "channel credentials are forbidden" in errors
    package["resources"]["channels"] = []
    assert "resource_counts mismatch" in validate_package(package)


def test_workspace_package_encoding_is_canonical():
    assert canonical_package({"b": 2, "a": 1}) == canonical_package({"a": 1, "b": 2})
