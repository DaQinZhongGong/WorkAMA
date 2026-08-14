from workama_platform.core import capability_allows, platform_key_scope_allows


def test_role_capability_wildcards_are_domain_scoped():
    assert capability_allows(("session:*",), "session:write")
    assert not capability_allows(("session:*",), "billing:read")
    assert capability_allows(("*",), "security:write")


def test_platform_key_read_scope_cannot_write():
    assert platform_key_scope_allows(("platform:read",), "billing:read")
    assert not platform_key_scope_allows(("platform:read",), "session:write")
    assert platform_key_scope_allows(("session:write",), "session:write")
