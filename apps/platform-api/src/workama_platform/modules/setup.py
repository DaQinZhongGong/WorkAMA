from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from workama_platform.core import hash_password, new_id, pool, settings


router = APIRouter(prefix="/api/v1/setup", tags=["setup"])


class BootstrapRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    organization_name: str = Field(min_length=1, max_length=100)
    workspace_name: str = Field(min_length=1, max_length=80)


async def _initialized() -> bool:
    async with pool.connection() as conn:
        result = await conn.execute("SELECT EXISTS(SELECT 1 FROM id_user)")
        row = await result.fetchone()
    return bool(row["exists"])


@router.get("/status")
async def setup_status():
    return {
        "initialized": await _initialized(),
        "setup_token_required": True,
        "external_backup_configured": False,
    }


@router.post("/bootstrap", status_code=status.HTTP_201_CREATED)
async def bootstrap(
    body: BootstrapRequest,
    x_setup_token: str = Header(default="", alias="X-Setup-Token"),
):
    if not settings.setup_token or not hmac.compare_digest(x_setup_token, settings.setup_token):
        raise HTTPException(status_code=403, detail="Invalid setup token")

    user_id, org_id, workspace_id = new_id("usr"), new_id("org"), new_id("wsp")
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(913006)")
            exists = await conn.execute("SELECT EXISTS(SELECT 1 FROM id_user)")
            if (await exists.fetchone())["exists"]:
                raise HTTPException(status_code=409, detail="WorkAMA is already initialized")
            await conn.execute(
                "INSERT INTO id_user(id,email,password_hash,display_name,email_verified) VALUES (%s,%s,%s,%s,TRUE)",
                (user_id, body.email.lower(), hash_password(body.password), body.display_name.strip()),
            )
            await conn.execute(
                "INSERT INTO id_org(id,name,owner_user_id) VALUES (%s,%s,%s)",
                (org_id, body.organization_name.strip(), user_id),
            )
            await conn.execute(
                "INSERT INTO id_workspace(id,org_id,name,slug) VALUES (%s,%s,%s,'default')",
                (workspace_id, org_id, body.workspace_name.strip()),
            )
            await conn.execute(
                "INSERT INTO id_member(id,org_id,workspace_id,user_id,role) VALUES (%s,%s,%s,%s,'owner')",
                (new_id("mem"), org_id, workspace_id, user_id),
            )
            await conn.execute(
                "INSERT INTO bill_account(id,workspace_id,granted_balance) VALUES (%s,%s,500)",
                (new_id("bacc"), workspace_id),
            )
            await conn.execute(
                """
                INSERT INTO bill_credit_grant(
                    id,workspace_id,source,period_start,expires_at,initial_amount,remaining_amount,idempotency_key
                ) VALUES (%s,%s,'initial',date_trunc('month',now()),date_trunc('month',now()) + interval '1 month',500,500,%s)
                ON CONFLICT(workspace_id,idempotency_key) DO NOTHING
                """,
                (new_id("grant"), workspace_id, f"initial:{workspace_id}"),
            )
            await conn.execute(
                "INSERT INTO gw_channel(id,workspace_id,name,provider,base_url,models,last_health) VALUES (%s,%s,'WorkAMA Local','mock','mock://local',ARRAY['workama-chat','workama-embed'],'healthy')",
                (new_id("chn"), workspace_id),
            )
            await conn.execute(
                "INSERT INTO gw_model_price(workspace_id,model,input_per_million,output_per_million,markup_percent) VALUES (%s,'workama-chat',1,2,10)",
                (workspace_id,),
            )
    return {"initialized": True, "email": body.email.lower(), "role": "owner", "workspace_id": workspace_id}
