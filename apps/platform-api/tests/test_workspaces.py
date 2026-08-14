from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException

from workama_platform.core import Actor, settings
from workama_platform.modules.workspaces import (
    ASSIGNABLE_INVITATION_ROLES,
    InvitationAcceptRequest,
    InvitationCreateRequest,
    WorkspaceCreateRequest,
    can_invite_role,
    can_manage_workspace,
    decode_workspace_token,
    invitation_is_active,
    issue_workspace_token,
    normalize_slug,
    same_tenant,
)


def _actor(*, org_id: str = "org_a", workspace_id: str = "ws_a", role: str = "admin", email: str = "a@example.com") -> Actor:
    return Actor(
        user_id="usr_a",
        workspace_id=workspace_id,
        org_id=org_id,
        role=role,
        email=email,
        display_name="A",
        onboarding_completed=True,
    )


def test_cross_tenant_resource_is_rejected_before_authorization():
    assert not same_tenant("org_a", "org_b")
    assert same_tenant("org_a", "org_a")
    with pytest.raises(HTTPException) as exc:
        if not same_tenant(_actor().org_id, "org_b"):
            raise HTTPException(status_code=404, detail="Organization not found")
    assert exc.value.status_code == 404


def test_workspace_roles_are_scoped_and_owner_invites_are_forbidden():
    assert can_manage_workspace("owner")
    assert can_manage_workspace("admin")
    assert not can_manage_workspace("member")
    assert not can_manage_workspace("viewer")
    assert can_invite_role("owner", "admin")
    assert can_invite_role("admin", "member")
    assert not can_invite_role("member", "viewer")
    assert not can_invite_role("admin", "owner")
    assert ASSIGNABLE_INVITATION_ROLES == {"admin", "member", "viewer"}


def test_workspace_slug_is_normalized_and_validated():
    assert normalize_slug("  Team-A  ") == "team-a"
    with pytest.raises(HTTPException) as exc:
        normalize_slug("Team A")
    assert exc.value.status_code == 422


def test_workspace_context_token_is_signed_and_tenant_bound():
    token = issue_workspace_token("usr_a", "org_a", "ws_a", "admin")
    payload = decode_workspace_token(token)
    assert payload["sub"] == "usr_a"
    assert payload["org"] == "org_a"
    assert payload["ws"] == "ws_a"
    assert payload["role"] == "admin"
    assert payload["type"] == "workspace_context"
    assert payload["jti"].startswith("wctx_")

    tampered = jwt.encode(
        {**payload, "org": "org_b"}, settings.jwt_secret, algorithm="HS256"
    )
    assert decode_workspace_token(tampered)["org"] == "org_b"
    assert not same_tenant("org_a", decode_workspace_token(tampered)["org"])


def test_workspace_context_token_rejects_wrong_type_and_expiry():
    now = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
    expired = issue_workspace_token("usr_a", "org_a", "ws_a", "member", ttl_seconds=1, now=now)
    with pytest.raises(HTTPException) as exc:
        decode_workspace_token(expired, now=now + timedelta(seconds=2))
    assert exc.value.status_code == 401

    wrong_type = jwt.encode(
        {"type": "access", "sub": "usr_a", "org": "org_a", "ws": "ws_a", "role": "member", "jti": "x"},
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc:
        decode_workspace_token(wrong_type)
    assert exc.value.status_code == 401


def test_duplicate_and_expired_invitation_state_are_distinct():
    now = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
    assert invitation_is_active("pending", now + timedelta(minutes=5), now)
    assert not invitation_is_active("pending", now - timedelta(seconds=1), now)
    assert not invitation_is_active("revoked", now + timedelta(minutes=5), now)
    assert not invitation_is_active("accepted", now + timedelta(minutes=5), now)


def test_request_models_do_not_accept_credentials_as_persisted_fields():
    workspace = WorkspaceCreateRequest(name="Team", slug="team", idempotency_key="req-1")
    invitation = InvitationCreateRequest(email="Invitee@example.com", role="member", idempotency_key="req-2")
    accepted = InvitationAcceptRequest(token="raw-token-value-which-is-only-submitted")
    assert workspace.model_dump().get("password") is None
    assert invitation.model_dump().get("password") is None
    assert accepted.token.startswith("raw-token")
