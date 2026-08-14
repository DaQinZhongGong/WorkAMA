from __future__ import annotations

import asyncio
import copy
import io
import json
import hashlib
import os
import secrets
import shutil
import tarfile
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import docker
import nats
from docker.errors import DockerException, NotFound
from fastapi import Body, Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from minio import Minio

from workama_sandbox.netpolicy import build_egress_rules


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql://workama:workama_dev@postgres:5432/workama"
    nats_url: str = "nats://nats:4222"
    internal_token: str = "change-this-internal-token"
    sandbox_image: str = "workama-sandbox-agentd:local"
    sandbox_browser_image: str = "workama-sandbox-browser:local"
    sandbox_code_image: str = "workama-sandbox-code:local"
    sandbox_provider: str = "docker"
    sandbox_runtime: str = "runsc"
    sandbox_firecracker_runtime: str = "kata-fc"
    sandbox_firecracker_binary: str = "/usr/local/bin/firecracker"
    sandbox_firecracker_kernel_image: str = ""
    sandbox_firecracker_rootfs_image: str = ""
    sandbox_firecracker_socket_dir: str = "/run/workama/firecracker"
    sandbox_firecracker_kvm_path: str = "/dev/kvm"
    sandbox_require_gvisor: bool = True
    sandbox_require_microvm: bool = False
    sandbox_idle_seconds: int = 900
    sandbox_ttl_seconds: int = 86400
    sandbox_memory: str = "4g"
    sandbox_nano_cpus: int = 2_000_000_000
    sandbox_prewarm_size: int = 2
    sandbox_max_total: int = 50
    sandbox_max_per_workspace: int = 20
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "workama"
    minio_secret_key: str = "workama_minio"


settings = Settings()
pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=5, open=False, kwargs={"row_factory": dict_row})


class _UnavailableDockerClient:
    def __getattr__(self, _name: str):
        raise DockerException("Docker socket is unavailable")


try:
    docker_client = docker.from_env()
except DockerException:
    docker_client = _UnavailableDockerClient()
nats_client = None
reaper_task: asyncio.Task | None = None
warm_task: asyncio.Task | None = None
warm_pool: list[tuple[Any, Any]] = []
warm_lock = asyncio.Lock()
object_store = Minio(settings.minio_endpoint, access_key=settings.minio_access_key, secret_key=settings.minio_secret_key, secure=False)
SNAPSHOT_BUCKET = "workama-sandboxes"


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(datetime.now(UTC).timestamp()*1000):013x}{secrets.token_hex(8)}"


def require_internal(x_internal_token: Annotated[str, Header()] = ""):
    if not secrets.compare_digest(x_internal_token, settings.internal_token):
        raise HTTPException(status_code=401, detail="Invalid internal service token")


class AcquireRequest(BaseModel):
    workspace_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,80}$")
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,80}$")
    image: str = "sandbox-base"
    scope_type: Literal["session", "workflow"] = "session"
    scope_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{3,80}$")


class ExecRequest(BaseModel):
    argv: list[str] = Field(min_length=1, max_length=32)
    timeout_seconds: int = Field(default=10, ge=1, le=120)


class FileWriteRequest(BaseModel):
    path: str
    content: str = Field(max_length=262144)


RuntimeIsolation = Literal["gvisor", "runc-dev", "firecracker", "unknown"]

_RUNTIME_OVERRIDE_ENV = "WORKAMA_SANDBOX_RUNTIME_OVERRIDE"
_VALID_OVERRIDES = {"gvisor", "runc-dev", "firecracker", "unknown"}


def effective_runtime() -> str:
    if settings.sandbox_provider == "firecracker":
        return settings.sandbox_firecracker_runtime
    return settings.sandbox_runtime


def runtime_available() -> tuple[bool, list[str]]:
    try:
        runtimes = list((docker_client.info().get("Runtimes") or {}).keys())
    except DockerException:
        return False, []
    return effective_runtime() in runtimes, runtimes


def _classify_runtime_name(name: str) -> RuntimeIsolation:
    """Map a Docker runtime identifier (e.g. ``runsc``) to a WorkAMA isolation label."""
    normalized = (name or "").strip().lower()
    if normalized == "runsc":
        return "gvisor"
    if normalized in {"kata-fc", "firecracker", "kata"}:
        return "firecracker"
    if normalized in {"runc", ""}:
        # Empty runtime resolves to the Docker default (``runc``); we treat that
        # as the local ``runc-dev`` profile so strict mode can reject it.
        return "runc-dev"
    return "unknown"


def _read_own_container_id() -> str:
    """Best-effort extraction of this container's ID from ``/proc/1/cgroup``.

    Returns an empty string when we are not running inside a container or the
    cgroup file cannot be parsed. Docker container IDs are 64-char hex strings;
    we accept any hex suffix of at least 12 chars to remain forward-compatible.
    """
    try:
        text = Path("/proc/1/cgroup").read_text(errors="ignore")
    except Exception:
        return ""
    for line in text.splitlines():
        parts = line.split("/")
        last = parts[-1].strip() if parts else ""
        # Some cgroup drivers suffix the id (e.g. ``docker-<id>.scope``); strip
        # those decorations before validating.
        for token in last.replace("-", " ").replace(".", " ").split():
            if len(token) >= 12 and all(c in "0123456789abcdef" for c in token.lower()):
                return token
    return ""


def detect_runtime_isolation() -> RuntimeIsolation:
    """Detect the Docker runtime isolating this process.

    Production deployments run the sandbox-fleet container under the ``runsc``
    (gVisor) runtime. Local development typically falls back to ``runc`` on
    Docker Desktop, which is reported as ``runc-dev``. The detection order is:

    1. ``WORKAMA_SANDBOX_RUNTIME_OVERRIDE`` env var (highest priority; useful
       for tests and explicit operator overrides).
    2. Docker socket: inspect this container's ``HostConfig.Runtime`` field.
    3. ``/proc/1/cgroup`` content (best-effort marker scan).
    4. ``"unknown"`` when no source yields a confident answer.
    """
    override = os.environ.get(_RUNTIME_OVERRIDE_ENV, "").strip().lower()
    if override in _VALID_OVERRIDES:
        return override  # type: ignore[return-value]

    container_id = _read_own_container_id()
    if container_id:
        try:
            info = docker_client.containers.get(container_id)
            runtime = (info.attrs.get("HostConfig") or {}).get("Runtime", "") or ""
            return _classify_runtime_name(runtime)
        except Exception:
            pass

    try:
        cgroup_text = Path("/proc/1/cgroup").read_text(errors="ignore")
    except Exception:
        cgroup_text = ""
    if "runsc" in cgroup_text:
        return "gvisor"
    if "kata-fc" in cgroup_text or "firecracker" in cgroup_text:
        return "firecracker"
    if "runc" in cgroup_text:
        return "runc-dev"

    return "unknown"


def enforce_strict_microvm(isolation: RuntimeIsolation | None = None) -> None:
    """Fail-closed guard for production gVisor requirement.

    When ``SANDBOX_REQUIRE_MICROVM=true`` is set in the environment, the fleet
    must refuse to operate unless ``detect_runtime_isolation()`` reports
    ``gvisor``. Any other isolation level (including ``firecracker`` and
    ``unknown``) raises ``RuntimeError`` so the HTTP layer can surface a 503.

    Local dev keeps ``SANDBOX_REQUIRE_MICROVM=false`` so Docker Desktop's
    ``runc`` runtime keeps working without breaking the loop.
    """
    if not settings.sandbox_require_microvm:
        return
    if isolation is None:
        isolation = detect_runtime_isolation()
    if isolation != "gvisor":
        raise RuntimeError("strict microVM required but runtime is not gVisor")


def firecracker_preflight(
    binary: str,
    socket_dir: str,
    kernel_image: str = "",
    rootfs_image: str = "",
    kvm_path: str = "/dev/kvm",
) -> dict[str, Any]:
    """Check host prerequisites without launching a VM or touching user data."""
    binary_path = shutil.which(binary)
    configured_binary = Path(binary)
    if binary_path is None and configured_binary.is_file() and os.access(configured_binary, os.X_OK):
        binary_path = str(configured_binary)
    socket_path = Path(socket_dir)
    kvm = Path(kvm_path)
    kernel = Path(kernel_image) if kernel_image else None
    rootfs = Path(rootfs_image) if rootfs_image else None
    checks = {
        "binary": bool(binary_path),
        "socket_dir": socket_path.is_dir() and os.access(socket_path, os.W_OK),
        "kvm": kvm.is_char_device() if hasattr(kvm, "is_char_device") else kvm.exists(),
        "kernel_image": kernel is not None and kernel.is_file(),
        "rootfs_image": rootfs is not None and rootfs.is_file(),
    }
    missing = [name for name, passed in checks.items() if not passed]
    return {
        "provider": "firecracker",
        "contract": "firecracker-api-v1",
        "status": "ready" if not missing else "pending_external",
        "ready": not missing,
        "checks": checks,
        "missing": missing,
        "binary": {"configured": binary, "resolved": binary_path},
        "socket_dir": str(socket_path),
        "kvm_path": str(kvm),
        "kernel_image_configured": bool(kernel_image),
        "rootfs_image_configured": bool(rootfs_image),
    }


def provider_status() -> dict[str, Any]:
    available, runtimes = runtime_available()
    preflight = firecracker_preflight(
        settings.sandbox_firecracker_binary,
        settings.sandbox_firecracker_socket_dir,
        settings.sandbox_firecracker_kernel_image,
        settings.sandbox_firecracker_rootfs_image,
        settings.sandbox_firecracker_kvm_path,
    )
    provider = settings.sandbox_provider
    if provider == "firecracker":
        provider_ready = available
        execution_mode = "docker-runtime"
        missing = [] if provider_ready else [f"docker_runtime:{effective_runtime()}"]
    elif provider in {"docker", "gvisor"}:
        provider_ready = available or not settings.sandbox_require_gvisor
        execution_mode = "docker-runtime" if available else "runc-dev"
        missing = [] if provider_ready else [f"docker_runtime:{effective_runtime()}"]
    else:
        provider_ready = False
        execution_mode = "unsupported"
        missing = [f"unsupported_provider:{provider}"]
    return {
        "provider": provider,
        "provider_ready": provider_ready,
        "execution_mode": execution_mode,
        "configured_runtime": effective_runtime(),
        "runtime_available": available,
        "available_runtimes": runtimes,
        "runtime_missing": missing,
        "gvisor_compliant": available and effective_runtime() == "runsc" and provider != "firecracker",
        "microvm_compliant": provider == "firecracker" and provider_ready,
        "microvm_required": settings.sandbox_require_microvm,
        "firecracker": {
            "managed_runtime_ready": provider == "firecracker" and available,
            "direct_preflight": preflight,
            "execution_contract": "docker-runtime/firecracker-compatible",
        },
    }


def container_options(sandbox_id: str, volume_name: str, *, workspace_id: str = "", session_id: str = "", warm: bool = False, image: str = "") -> dict[str, Any]:
    available,_=runtime_available()
    if settings.sandbox_provider == "firecracker" and not available:
        raise RuntimeError(f"Firecracker-compatible Docker runtime is unavailable: {effective_runtime()}")
    if settings.sandbox_require_gvisor and not available:
        raise RuntimeError(f"required gVisor runtime is unavailable: {effective_runtime()}")
    labels={"workama.sandbox":sandbox_id,"workama.warm":str(warm).lower()}
    if workspace_id: labels["workama.workspace"]=workspace_id
    if session_id: labels["workama.session"]=session_id
    startup="chown 10001:10001 /workspace && chmod 0750 /workspace && exec setpriv --reuid=10001 --regid=10001 --clear-groups --no-new-privs /usr/local/bin/sandbox-agentd serve"
    # 根据 image 选择 Docker 镜像与网络出网策略（见 netpolicy.build_egress_rules）
    egress=build_egress_rules(image=image or "sandbox-base")
    if image=="sandbox-browser":
        docker_image=settings.sandbox_browser_image
    elif image=="sandbox-code":
        docker_image=settings.sandbox_code_image
    else:
        docker_image=settings.sandbox_image
    if egress.get("network_mode")=="none":
        network_arg={"network_mode":"none"}
    else:
        network_arg={"network":egress["network"]}
    options=dict(image=docker_image,name=f"workama-{'warm-' if warm else ''}{sandbox_id}",detach=True,entrypoint="/bin/sh",command=["-c",startup],read_only=True,user="0:0",mem_limit=settings.sandbox_memory,nano_cpus=settings.sandbox_nano_cpus,pids_limit=256,cap_drop=["ALL"],cap_add=["CHOWN","FOWNER","SETUID","SETGID"],security_opt=["no-new-privileges:true"],tmpfs={"/tmp":"rw,noexec,nosuid,size=64m"},volumes={volume_name:{"bind":"/workspace","mode":"rw"}},labels=labels,**network_arg)
    if available: options["runtime"]=effective_runtime()
    return options


async def create_container(sandbox_id: str, *, workspace_id: str = "", session_id: str = "", warm: bool = False, image: str = "sandbox-base"):
    volume_name=f"workama-sandbox-{secrets.token_hex(10)}"; volume=await asyncio.to_thread(docker_client.volumes.create,name=volume_name,labels={"workama.sandbox":sandbox_id,"workama.warm":str(warm).lower()})
    container=None
    try:
        container=await asyncio.to_thread(docker_client.containers.run,**container_options(sandbox_id,volume.name,workspace_id=workspace_id,session_id=session_id,warm=warm,image=image))
        await wait_agentd(container)
    except Exception:
        if container is not None:
            try: await asyncio.to_thread(container.remove,force=True)
            except Exception: pass
        await asyncio.to_thread(volume.remove,force=True); raise
    return container,volume


async def agentd_call(container: Any, method: str, payload: dict[str, Any] | None = None, *, attempts: int = 1) -> dict[str, Any]:
    command=["/usr/local/bin/sandbox-agentd","client",method,json.dumps(payload or {},separators=(",",":"))]
    last_error="sandbox-agentd is unavailable"
    for attempt in range(attempts):
        result=await asyncio.to_thread(container.exec_run,command,user="10001:10001",demux=True)
        stdout,stderr=result.output if isinstance(result.output,tuple) else (result.output,b"")
        if result.exit_code==0:
            return json.loads((stdout or b"{}").decode())
        last_error=(stderr or stdout or b"sandbox-agentd call failed").decode(errors="replace").strip()
        if attempt+1<attempts: await asyncio.sleep(0.05)
    raise RuntimeError(last_error[:1000])


async def wait_agentd(container: Any) -> dict[str, Any]:
    return await agentd_call(container,"Health",attempts=40)


async def maintain_warm_pool():
    while True:
        try:
            async with warm_lock:
                while len(warm_pool)<settings.sandbox_prewarm_size:
                    warm_pool.append(await create_container(new_id("warm"),warm=True))
            await asyncio.sleep(5)
        except asyncio.CancelledError: raise
        except Exception: await asyncio.sleep(5)


async def cleanup_warm_containers():
    async with pool.connection() as conn:
        result=await conn.execute("SELECT container_id FROM ag_sandbox WHERE status IN ('active','sleeping')"); claimed={row["container_id"] for row in await result.fetchall()}
    for container in await asyncio.to_thread(docker_client.containers.list,all=True,filters={"label":"workama.warm=true"}):
        if container.id in claimed: continue
        volume_names=[mount.get("Name") for mount in container.attrs.get("Mounts",[]) if mount.get("Name")]
        try: await asyncio.to_thread(container.remove,force=True)
        except Exception: pass
        for name in volume_names:
            try: await asyncio.to_thread(docker_client.volumes.get(name).remove,force=True)
            except Exception: pass
    warm_pool.clear()


async def claim_container(sandbox_id: str, workspace_id: str, session_id: str, image: str = "sandbox-base"):
    # 仅 sandbox-base 镜像使用预热池；sandbox-browser 等非默认镜像直接冷启动
    if image == "sandbox-base":
        async with warm_lock:
            if warm_pool:
                container,volume=warm_pool.pop(0)
                asyncio.create_task(_replenish_once())
                return container,volume,"prewarmed"
    container,volume=await create_container(sandbox_id,workspace_id=workspace_id,session_id=session_id,image=image)
    return container,volume,"cold"


async def _replenish_once():
    try:
        container,volume=await create_container(new_id("warm"),warm=True)
        async with warm_lock:
            if len(warm_pool)<settings.sandbox_prewarm_size: warm_pool.append((container,volume)); return
        await asyncio.to_thread(container.remove,force=True); await asyncio.to_thread(volume.remove,force=True)
    except Exception: return


async def put_snapshot(row: dict[str,Any]) -> tuple[str,str,int]:
    container=docker_client.containers.get(row["container_id"]); stream,_=await asyncio.to_thread(container.get_archive,"/workspace")
    source=io.BytesIO(b"".join(stream)); normalized=io.BytesIO()
    with tarfile.open(fileobj=source,mode="r:*") as incoming, tarfile.open(fileobj=normalized,mode="w") as outgoing:
        for member in incoming:
            parts=member.name.replace("\\","/").split("/",1)
            if len(parts)==1:
                continue
            item=copy.copy(member); item.name=parts[1]
            payload=incoming.extractfile(member) if member.isfile() else None
            outgoing.addfile(item,payload)
    data=normalized.getvalue(); digest=hashlib.sha256(data).hexdigest(); key=f"sandboxes/{row['workspace_id']}/{row['id']}/fs-v2.tar"
    def upload():
        if not object_store.bucket_exists(SNAPSHOT_BUCKET): object_store.make_bucket(SNAPSHOT_BUCKET)
        object_store.put_object(SNAPSHOT_BUCKET,key,io.BytesIO(data),len(data),content_type="application/x-tar")
    await asyncio.to_thread(upload); return key,digest,len(data)


async def restore_snapshot(container, row: dict[str,Any]) -> bool:
    key=row.get("snapshot_s3_key")
    if not key: return False
    response=await asyncio.to_thread(object_store.get_object,SNAPSHOT_BUCKET,key)
    try: data=await asyncio.to_thread(response.read)
    finally: response.close(); response.release_conn()
    if hashlib.sha256(data).hexdigest()!=row.get("snapshot_sha256"): raise RuntimeError("sandbox snapshot checksum mismatch")
    return bool(await asyncio.to_thread(container.put_archive,"/workspace",data))


async def ensure_schema():
    async with pool.connection() as conn:
        await conn.execute("""CREATE TABLE IF NOT EXISTS ag_sandbox(
            id TEXT PRIMARY KEY,session_id TEXT REFERENCES ag_session(id) ON DELETE CASCADE,
            scope_type TEXT NOT NULL DEFAULT 'session',scope_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL REFERENCES id_workspace(id),runtime TEXT NOT NULL,image TEXT NOT NULL,
            container_id TEXT NOT NULL,volume_name TEXT NOT NULL,status TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at TIMESTAMPTZ,meter_seconds BIGINT NOT NULL DEFAULT 0)""")
        await conn.execute("ALTER TABLE ag_sandbox ALTER COLUMN session_id DROP NOT NULL")
        await conn.execute("ALTER TABLE ag_sandbox DROP CONSTRAINT IF EXISTS ag_sandbox_session_id_fkey")
        await conn.execute("ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS scope_type TEXT NOT NULL DEFAULT 'session'")
        await conn.execute("ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS scope_id TEXT")
        await conn.execute("UPDATE ag_sandbox SET scope_id=session_id WHERE scope_id IS NULL")
        await conn.execute("ALTER TABLE ag_sandbox ALTER COLUMN scope_id SET NOT NULL")
        await conn.execute("ALTER TABLE ag_sandbox DROP CONSTRAINT IF EXISTS ag_sandbox_scope_type_check")
        await conn.execute("ALTER TABLE ag_sandbox ADD CONSTRAINT ag_sandbox_scope_type_check CHECK (scope_type IN ('session','workflow'))")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ag_sandbox_active_session ON ag_sandbox(session_id) WHERE status IN ('active','sleeping')")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ag_sandbox_active_scope ON ag_sandbox(scope_type,scope_id) WHERE status IN ('active','sleeping')")
        await conn.execute("ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS snapshot_s3_key TEXT")
        await conn.execute("ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS snapshot_sha256 TEXT")
        await conn.execute("ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS snapshot_size_bytes BIGINT")
        await conn.execute("ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS snapshotted_at TIMESTAMPTZ")
        await conn.execute("ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS restore_count INTEGER NOT NULL DEFAULT 0")
        await conn.execute("ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS allocation_source TEXT NOT NULL DEFAULT 'cold'")
        await conn.execute("ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS cold_start_ms INTEGER")
        await conn.execute("ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'docker'")
        await conn.execute("ALTER TABLE ag_sandbox ADD COLUMN IF NOT EXISTS runtime_contract TEXT NOT NULL DEFAULT 'docker-runtime'")
        await conn.commit()


async def reconcile_sandboxes():
    """Repair database state after Docker resources disappear or the fleet restarts."""
    now = datetime.now(UTC)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ag_sandbox WHERE status IN ('active','sleeping')")
        rows = await result.fetchall()
        for row in rows:
            try:
                container = await asyncio.to_thread(docker_client.containers.get, row["container_id"])
                await asyncio.to_thread(container.reload)
            except NotFound:
                if row["status"] == "sleeping" and row.get("snapshot_s3_key"):
                    # A durable snapshot is intentionally recoverable without occupying capacity.
                    continue
                await conn.execute(
                    "UPDATE ag_sandbox SET status='released',ended_at=%s WHERE id=%s",
                    (now, row["id"]),
                )
                continue
            if row["status"] == "active" and container.status != "running":
                await conn.execute(
                    "UPDATE ag_sandbox SET status='released',ended_at=%s WHERE id=%s",
                    (now, row["id"]),
                )
        await conn.commit()


async def emit_meter(row: dict[str, Any], ended: datetime):
    global nats_client
    if nats_client is None:
        return
    started = row["last_active_at"] or row["started_at"]
    seconds = max(1, int((ended - started).total_seconds()))
    payload = {"schema_version": "1.0", "event_id": new_id("evt"), "event_type": "metering.sandbox.v1", "occurred_at": ended.isoformat(), "producer": "sandbox-fleet", "workspace_id": row["workspace_id"], "idempotency_key": f"{row['id']}:{int(ended.timestamp())}", "classification": "C1", "payload": {"sandbox_id": row["id"], "session_id": row["session_id"], "runtime": row["runtime"], "seconds": seconds}}
    await nats_client.publish("metering.sandbox.v1", json.dumps(payload).encode())


def elapsed_seconds(row: dict[str, Any], ended: datetime) -> int:
    started = row["last_active_at"] or row["started_at"]
    return max(1, int((ended - started).total_seconds()))


async def record_meter(conn, row: dict[str, Any], ended: datetime):
    seconds = elapsed_seconds(row, ended)
    await conn.execute(
        "UPDATE ag_sandbox SET meter_seconds=meter_seconds+%s,last_active_at=%s WHERE id=%s",
        (seconds, ended, row["id"]),
    )
    await emit_meter(row, ended)


async def reaper():
    while True:
        await asyncio.sleep(min(max(settings.sandbox_idle_seconds // 3, 5), 60))
        now = datetime.now(UTC)
        async with pool.connection() as conn:
            result = await conn.execute("SELECT * FROM ag_sandbox WHERE status IN ('active','sleeping')")
            rows = await result.fetchall()
            for row in rows:
                age = (now - row["started_at"]).total_seconds()
                idle = (now - row["last_active_at"]).total_seconds()
                try:
                    container = docker_client.containers.get(row["container_id"])
                    if age >= settings.sandbox_ttl_seconds:
                        container.remove(force=True)
                        docker_client.volumes.get(row["volume_name"]).remove(force=True)
                        await conn.execute("UPDATE ag_sandbox SET status='released',ended_at=%s WHERE id=%s", (now, row["id"]))
                        await record_meter(conn, row, now)
                    elif row["status"] == "active" and idle >= settings.sandbox_idle_seconds:
                        key,digest,size=await put_snapshot(row)
                        container.stop(timeout=2)
                        await conn.execute("UPDATE ag_sandbox SET status='sleeping',snapshot_s3_key=%s,snapshot_sha256=%s,snapshot_size_bytes=%s,snapshotted_at=now() WHERE id=%s",(key,digest,size,row["id"]))
                        await record_meter(conn, row, now)
                except NotFound:
                    await conn.execute("UPDATE ag_sandbox SET status='failed',ended_at=%s WHERE id=%s", (now, row["id"]))
            await conn.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global nats_client, reaper_task, warm_task
    await pool.open(); await ensure_schema(); await reconcile_sandboxes(); await cleanup_warm_containers()
    try: nats_client = await nats.connect(settings.nats_url, connect_timeout=3)
    except Exception: nats_client = None
    reaper_task = asyncio.create_task(reaper())
    capabilities = await asyncio.to_thread(provider_status)
    warm_task = asyncio.create_task(maintain_warm_pool()) if capabilities["provider_ready"] else None
    yield
    reaper_task.cancel()
    if warm_task is not None:
        warm_task.cancel()
    await cleanup_warm_containers()
    if nats_client: await nats_client.close()
    await pool.close()


app = FastAPI(title="WorkAMA Sandbox Fleet", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    isolation = await asyncio.to_thread(detect_runtime_isolation)
    try:
        enforce_strict_microvm(isolation)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    capabilities = await asyncio.to_thread(provider_status)
    status = "ok"
    if settings.sandbox_require_gvisor and not capabilities["gvisor_compliant"]:
        status = "degraded"
    if settings.sandbox_require_microvm and not capabilities["microvm_compliant"]:
        status = "degraded"
    async with pool.connection() as conn:
        count=(await (await conn.execute("SELECT count(*) count FROM ag_sandbox WHERE status='active'")).fetchone())["count"]
    return {"status":status,"service":"sandbox-fleet","runtime_isolation":isolation,"strict_microvm_enforced":settings.sandbox_require_microvm,**capabilities,"prewarm":{"ready":len(warm_pool),"target":settings.sandbox_prewarm_size},"capacity":{"active":count,"maximum":settings.sandbox_max_total}}


@app.get("/internal/runtime-capabilities", dependencies=[Depends(require_internal)])
async def runtime_capabilities():
    return await asyncio.to_thread(provider_status)


async def get_row(sandbox_id: str) -> dict[str, Any]:
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ag_sandbox WHERE id=%s", (sandbox_id,))
        row = await result.fetchone()
    if not row: raise HTTPException(status_code=404, detail="Sandbox not found")
    return row


@app.get("/internal/sandboxes", dependencies=[Depends(require_internal)])
async def find_sandbox(session_id: str, workspace_id: str):
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM ag_sandbox WHERE session_id=%s AND workspace_id=%s ORDER BY started_at DESC LIMIT 1",
            (session_id, workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Sandbox not found")
    return {**row, "gvisor_compliant": row["runtime"] == "runsc", "microvm_compliant": row.get("provider") == "firecracker"}


@app.post("/internal/sandboxes", dependencies=[Depends(require_internal)])
async def acquire(body: AcquireRequest):
    capabilities = await asyncio.to_thread(provider_status)
    available = capabilities["runtime_available"]
    if not capabilities["provider_ready"]:
        raise HTTPException(status_code=503, detail=f"E06001: sandbox provider is unavailable ({','.join(capabilities['runtime_missing'])})")
    if settings.sandbox_require_gvisor and not capabilities["gvisor_compliant"]:
        raise HTTPException(status_code=503, detail="E06001: required gVisor runtime is unavailable")
    if settings.sandbox_require_microvm and not capabilities["microvm_compliant"]:
        raise HTTPException(status_code=503, detail="E06003: required Firecracker-compatible microVM runtime is unavailable")
    scope_id = body.scope_id or body.session_id
    session_id = body.session_id if body.scope_type == "session" else None
    async with pool.connection() as conn:
        if body.scope_type == "session":
            session_result = await conn.execute(
                "SELECT 1 FROM ag_session WHERE id=%s AND workspace_id=%s",
                (body.session_id, body.workspace_id),
            )
            if not await session_result.fetchone():
                raise HTTPException(status_code=404, detail="Sandbox session not found")
        result = await conn.execute("SELECT * FROM ag_sandbox WHERE scope_type=%s AND scope_id=%s AND workspace_id=%s AND status IN ('active','sleeping')", (body.scope_type, scope_id, body.workspace_id))
        existing = await result.fetchone()
        if existing:
            if existing["status"] == "sleeping":
                try:
                    container=docker_client.containers.get(existing["container_id"]); await asyncio.to_thread(container.start); await wait_agentd(container)
                except NotFound:
                    started=time.perf_counter(); container,volume,source=await claim_container(existing["id"],body.workspace_id,scope_id,existing.get("image") or "sandbox-base"); restored=await restore_snapshot(container,existing)
                    await conn.execute("UPDATE ag_sandbox SET container_id=%s,volume_name=%s,allocation_source=%s,cold_start_ms=%s,restore_count=restore_count+%s WHERE id=%s",(container.id,volume.name,source,int((time.perf_counter()-started)*1000),1 if restored else 0,existing["id"]))
                    existing["container_id"],existing["volume_name"],existing["allocation_source"]=container.id,volume.name,source
                await conn.execute("UPDATE ag_sandbox SET status='active',last_active_at=now() WHERE id=%s", (existing["id"],)); await conn.commit()
                existing["status"] = "active"
            return {**existing,"restored":True,"snapshot_restored":bool(existing.get("snapshot_s3_key")),"gvisor_compliant":existing["runtime"]=="runsc","microvm_compliant":existing.get("provider")=="firecracker"}
        capacity=await conn.execute("SELECT count(*) FILTER (WHERE status='active') total,count(*) FILTER (WHERE workspace_id=%s AND status='active') workspace FROM ag_sandbox",(body.workspace_id,)); counts=await capacity.fetchone()
        if counts["total"]>=settings.sandbox_max_total or counts["workspace"]>=settings.sandbox_max_per_workspace: raise HTTPException(status_code=503,detail="E06001: sandbox capacity is exhausted")
    sandbox_id=new_id("sbx"); started=time.perf_counter()
    try: container,volume,source=await claim_container(sandbox_id,body.workspace_id,scope_id,body.image)
    except DockerException as exc: raise HTTPException(status_code=503,detail=f"E06001: sandbox allocation failed: {exc}") from exc
    runtime = settings.sandbox_runtime if available else "runc-dev"
    async with pool.connection() as conn:
        cold_ms=int((time.perf_counter()-started)*1000); await conn.execute("INSERT INTO ag_sandbox(id,session_id,scope_type,scope_id,workspace_id,provider,runtime,runtime_contract,image,container_id,volume_name,status,allocation_source,cold_start_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s)",(sandbox_id,session_id,body.scope_type,scope_id,body.workspace_id,settings.sandbox_provider,runtime,capabilities["firecracker"]["execution_contract"] if settings.sandbox_provider == "firecracker" else "docker-runtime",body.image,container.id,volume.name,source,cold_ms)); await conn.commit()
    return {"id":sandbox_id,"session_id":session_id,"scope_type":body.scope_type,"scope_id":scope_id,"workspace_id":body.workspace_id,"provider":settings.sandbox_provider,"runtime":runtime,"runtime_contract":capabilities["firecracker"]["execution_contract"] if settings.sandbox_provider == "firecracker" else "docker-runtime","status":"active","restored":False,"allocation_source":source,"cold_start_ms":cold_ms,"gvisor_compliant":runtime=="runsc","microvm_compliant":settings.sandbox_provider=="firecracker"}


@app.post("/internal/sandboxes/{sandbox_id}/exec", dependencies=[Depends(require_internal)])
async def execute(sandbox_id: str, body: ExecRequest):
    row = await get_row(sandbox_id)
    if row["status"] != "active": raise HTTPException(status_code=409, detail="Sandbox is not active")
    try: result=await agentd_call(docker_client.containers.get(row["container_id"]),"Exec",{"argv":body.argv,"timeout_seconds":body.timeout_seconds})
    except (NotFound,RuntimeError) as exc: raise HTTPException(status_code=503,detail=f"E06002: sandbox-agentd execution failed: {exc}") from exc
    async with pool.connection() as conn: await conn.execute("UPDATE ag_sandbox SET last_active_at=now() WHERE id=%s", (sandbox_id,)); await conn.commit()
    return result


@app.put("/internal/sandboxes/{sandbox_id}/files", dependencies=[Depends(require_internal)])
async def write_file(sandbox_id: str, body: FileWriteRequest):
    row = await get_row(sandbox_id); path = body.path.replace("\\", "/").lstrip("/")
    if not path or ".." in path.split("/"): raise HTTPException(status_code=400, detail="Path escapes workspace")
    try: result=await agentd_call(docker_client.containers.get(row["container_id"]),"WriteFile",{"path":path,"content":body.content})
    except (NotFound,RuntimeError) as exc: raise HTTPException(status_code=503,detail=f"E06002: sandbox-agentd file write failed: {exc}") from exc
    async with pool.connection() as conn: await conn.execute("UPDATE ag_sandbox SET last_active_at=now() WHERE id=%s", (sandbox_id,)); await conn.commit()
    return result


@app.get("/internal/sandboxes/{sandbox_id}/files", dependencies=[Depends(require_internal)])
async def read_file(sandbox_id: str, path: str):
    row=await get_row(sandbox_id); clean=path.replace("\\", "/").lstrip("/")
    if not clean or ".." in clean.split("/"): raise HTTPException(status_code=400, detail="Path escapes workspace")
    try: return await agentd_call(docker_client.containers.get(row["container_id"]),"ReadFile",{"path":clean})
    except NotFound as exc: raise HTTPException(status_code=404,detail="File not found") from exc
    except RuntimeError as exc:
        if "NotFound" in str(exc): raise HTTPException(status_code=404,detail="File not found") from exc
        raise HTTPException(status_code=503,detail=f"E06002: sandbox-agentd file read failed: {exc}") from exc


@app.post("/internal/sandboxes/{sandbox_id}/sleep", dependencies=[Depends(require_internal)])
async def sleep_sandbox(sandbox_id: str):
    row = await get_row(sandbox_id)
    if row["status"] == "sleeping":
        return {"id": sandbox_id, "status": "sleeping"}
    if row["status"] != "active":
        raise HTTPException(status_code=409, detail="Sandbox cannot be suspended")
    now = datetime.now(UTC)
    try:
        key,digest,size=await put_snapshot(row)
        await asyncio.to_thread(docker_client.containers.get(row["container_id"]).stop, timeout=2)
    except NotFound as exc:
        raise HTTPException(status_code=409, detail="Sandbox container is missing") from exc
    async with pool.connection() as conn:
        await conn.execute("UPDATE ag_sandbox SET status='sleeping',snapshot_s3_key=%s,snapshot_sha256=%s,snapshot_size_bytes=%s,snapshotted_at=now() WHERE id=%s",(key,digest,size,sandbox_id))
        await record_meter(conn, row, now)
        await conn.commit()
    return {"id":sandbox_id,"status":"sleeping","snapshot_s3_key":key,"snapshot_sha256":digest,"snapshot_size_bytes":size}


@app.delete("/internal/sandboxes/{sandbox_id}", dependencies=[Depends(require_internal)])
async def release(sandbox_id: str):
    row=await get_row(sandbox_id); now=datetime.now(UTC)
    try: await asyncio.to_thread(docker_client.containers.get(row["container_id"]).remove, force=True)
    except NotFound: pass
    try: await asyncio.to_thread(docker_client.volumes.get(row["volume_name"]).remove, force=True)
    except NotFound: pass
    if row.get("snapshot_s3_key"):
        try: await asyncio.to_thread(object_store.remove_object,SNAPSHOT_BUCKET,row["snapshot_s3_key"])
        except Exception: pass
    async with pool.connection() as conn:
        await conn.execute("UPDATE ag_sandbox SET status='released',ended_at=%s WHERE id=%s", (now, sandbox_id))
        await record_meter(conn, row, now)
        await conn.commit()
    return {"id":sandbox_id,"status":"released"}


@app.websocket("/internal/sandboxes/{sandbox_id}/terminal/stream")
async def terminal_stream(websocket: WebSocket, sandbox_id: str):
    """桥接客户端 WebSocket 与沙箱内 sandbox-agentd 的 ExecStream gRPC 双向流。

    协议：
    - 客户端→服务端：JSON 文本消息（start/input/resize/signal）
    - 服务端→客户端：JSON 文本消息（output/exit）

    关闭码：
    - 4004：沙箱不存在
    - 4009：沙箱非活跃状态
    - 1011：服务器内部错误
    """
    await websocket.accept()
    sock = None
    ws_to_sock_task = None
    sock_to_ws_task = None
    try:
        # 获取沙箱元数据
        try:
            row = await get_row(sandbox_id)
        except HTTPException:
            await websocket.close(code=4004)
            return
        if row["status"] != "active":
            await websocket.close(code=4009)
            return
        # 启动持久 agentd stream 子进程，获得 docker exec 的 raw socket
        container = docker_client.containers.get(row["container_id"])
        exec_result = await asyncio.to_thread(
            container.exec_run,
            ["/usr/local/bin/sandbox-agentd", "stream"],
            user="10001:10001",
            stdin=True,
            socket=True,
            demux=False,
        )
        sock = exec_result.output
        sock.setblocking(False)
        loop = asyncio.get_running_loop()

        async def ws_to_sock():
            """WebSocket→docker exec socket：读取 WS JSON 消息，写入 socket。"""
            try:
                while True:
                    msg = await websocket.receive_text()
                    await loop.sock_sendall(sock, (msg + "\n").encode())
            except WebSocketDisconnect:
                return

        async def sock_to_ws():
            """docker exec socket→WebSocket：按行读取 stdout JSON，转发到 WS。"""
            try:
                buffer = b""
                while True:
                    data = await loop.sock_recv(sock, 65536)
                    if not data:
                        break
                    buffer += data
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line.strip():
                            await websocket.send_text(line.decode())
            except (WebSocketDisconnect, ConnectionError):
                return

        ws_to_sock_task = asyncio.create_task(ws_to_sock())
        sock_to_ws_task = asyncio.create_task(sock_to_ws())
        done, pending = await asyncio.wait(
            {ws_to_sock_task, sock_to_ws_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        # 消费已完成 task 的异常以避免 asyncio 警告
        for t in done:
            if not t.cancelled():
                t.exception()
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        if ws_to_sock_task and not ws_to_sock_task.done():
            ws_to_sock_task.cancel()
        if sock_to_ws_task and not sock_to_ws_task.done():
            sock_to_ws_task.cancel()
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
            # 只有在确实建立了流连接时才更新活跃时间
            try:
                async with pool.connection() as conn:
                    await conn.execute("UPDATE ag_sandbox SET last_active_at=now() WHERE id=%s", (sandbox_id,))
                    await conn.commit()
            except Exception:
                pass


@app.post("/internal/sandboxes/{sandbox_id}/browser", dependencies=[Depends(require_internal)])
async def browser_op(sandbox_id: str, body: dict[str, Any] = Body(default={})):
    """调用 sandbox-agentd 的 BrowserOp unary RPC。

    请求体透传给 sandbox-agentd，常见字段：
    - action: navigate / click / input / screenshot / eval / wait_for / close
    - target: URL（navigate）或 CSS 选择器（click/input/wait_for）
    - value: 输入文本（input）或 JS 脚本（eval）
    - timeout_ms: 超时毫秒数
    """
    row = await get_row(sandbox_id)
    if row["status"] != "active": raise HTTPException(status_code=409, detail="Sandbox is not active")
    try: result=await agentd_call(docker_client.containers.get(row["container_id"]),"BrowserOp",body)
    except (NotFound,RuntimeError) as exc: raise HTTPException(status_code=503,detail=f"E06002: sandbox-agentd browser op failed: {exc}") from exc
    async with pool.connection() as conn: await conn.execute("UPDATE ag_sandbox SET last_active_at=now() WHERE id=%s", (sandbox_id,)); await conn.commit()
    return result
