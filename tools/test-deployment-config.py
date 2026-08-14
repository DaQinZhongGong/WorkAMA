#!/usr/bin/env python3
"""WorkAMA production deployment config validation tests.

This script validates that the production deployment artefacts under
``deploy/helm/workama/`` and ``docs/`` are present, syntactically valid and
satisfy the production baseline declared in ``WorkAMA-Docs/910-进度追踪与
任务清单.md`` (v7.160). It does NOT require the ``helm`` CLI - all checks
are pure-Python YAML / text assertions.

Artefacts covered:
  - deploy/helm/workama/values-production.yaml  (production overrides)
  - deploy/helm/workama/values-staging.yaml   (staging overrides)
  - deploy/helm/workama/values.yaml            (default values, sanity)
  - deploy/helm/workama/Chart.yaml             (chart metadata, sanity)
  - deploy/compose/.env.production.template    (env template)
  - docs/deployment-guide.md                   (Helm deploy doc)
  - docs/docker-compose-production.md          (Compose deploy doc)

Test categories (>= 10 assertions):
  1.  YAML syntax: values-production.yaml parses
  2.  YAML syntax: values-staging.yaml parses
  3.  Production replicas >= 2 for every enabled appService
  4.  Production HPA minReplicas >= 3 for platform-api / gateway / web
  5.  Production resources.limits defined for every enabled appService
  6.  Production affinity + topologySpreadConstraints present
  7.  Production networkPolicy.enabled == true
  8.  Production storage values (postgres 100Gi, redis 20Gi, minio 500Gi)
  9.  Production tolerations defined
  10. .env.production.template contains all required variables
  11. docs/deployment-guide.md exists and has all required sections
  12. docs/docker-compose-production.md exists and has all required sections
  13. Staging replicas + HPA ceiling (lighter than production)
  14. Secrets placeholders present in production values (no real secrets)

Exit code: 0 if all assertions pass, 1 otherwise. Evidence is written to
``quality/evidence/test-deployment-config.json``.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a project dependency
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "workama"
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
DOCS_DIR = REPO_ROOT / "docs"
EVIDENCE_FILE = REPO_ROOT / "quality" / "evidence" / "test-deployment-config.json"

VALUES_PRODUCTION = CHART_DIR / "values-production.yaml"
VALUES_STAGING = CHART_DIR / "values-staging.yaml"
VALUES_DEFAULT = CHART_DIR / "values.yaml"
CHART_FILE = CHART_DIR / "Chart.yaml"
ENV_TEMPLATE = COMPOSE_DIR / ".env.production.template"
DEPLOYMENT_GUIDE = DOCS_DIR / "deployment-guide.md"
COMPOSE_DOC = DOCS_DIR / "docker-compose-production.md"

# Services that must ship a strict (>= 3) HPA floor in production - they are
# the user-facing stateless services that need cross-AZ spreading.
STRICT_HPA_SERVICES = ("platform-api", "gateway", "web")
# Required env vars in .env.production.template (subset - the must-haves).
REQUIRED_ENV_VARS = (
    "WORKAMA_ENV",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "JWT_SECRET",
    "INTERNAL_TOKEN",
    "KEY_PEPPER",
    "ENCRYPTION_KEY",
    "GRAFANA_ADMIN_PASSWORD",
    "SANDBOX_REQUIRE_MICROVM",
    "CORS_ORIGINS",
    "WORKAMA_PASSKEY_RP_ID",
    "WORKAMA_PASSKEY_ORIGIN",
)
# Required section anchors in docs/deployment-guide.md (H1 or H2 headings).
REQUIRED_DEPLOYMENT_SECTIONS = (
    "环境要求",
    "快速部署",
    "配置说明",
    "密钥管理",
    "存储配置",
    "网络配置",
    "监控配置",
    "升级指南",
    "备份恢复",
    "故障排查",
)
# Required section anchors in docs/docker-compose-production.md.
REQUIRED_COMPOSE_SECTIONS = (
    "适用场景",
    "环境要求",
    "快速部署",
    "备份",
    "监控",
    "升级",
    "故障排查",
)


class CheckResult:
    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name = name
        self.ok = ok
        self.detail = detail

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def check(name: str, ok: bool, detail: str = "") -> CheckResult:
    return CheckResult(name, bool(ok), detail)


def load_yaml(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8") as fh:
        return fh.read()


def has_limits(resources: dict) -> bool:
    if not isinstance(resources, dict):
        return False
    limits = resources.get("limits") or {}
    return bool(limits.get("cpu") and limits.get("memory"))


def has_requests(resources: dict) -> bool:
    if not isinstance(resources, dict):
        return False
    requests = resources.get("requests") or {}
    return bool(requests.get("cpu") and requests.get("memory"))


# ---------------------------------------------------------------------------
# Test 1: values-production.yaml parses as valid YAML
# ---------------------------------------------------------------------------
def test_values_production_yaml_syntax(results: list) -> dict:
    try:
        data = load_yaml(VALUES_PRODUCTION)
    except Exception as exc:  # pragma: no cover - syntax error path
        results.append(check("values_production.yaml_parses", False, str(exc)))
        return {}
    ok = isinstance(data, dict) and "appServices" in data and "global" in data
    results.append(check(
        "values_production.yaml_parses",
        ok,
        f"top_keys={sorted((data or {}).keys())[:6]}" if ok else "missing appServices/global",
    ))
    return data or {}


# ---------------------------------------------------------------------------
# Test 2: values-staging.yaml parses as valid YAML
# ---------------------------------------------------------------------------
def test_values_staging_yaml_syntax(results: list) -> dict:
    try:
        data = load_yaml(VALUES_STAGING)
    except Exception as exc:  # pragma: no cover - syntax error path
        results.append(check("values_staging.yaml_parses", False, str(exc)))
        return {}
    ok = isinstance(data, dict) and "appServices" in data and "global" in data
    results.append(check(
        "values_staging.yaml_parses",
        ok,
        f"top_keys={sorted((data or {}).keys())[:6]}" if ok else "missing appServices/global",
    ))
    return data or {}


# ---------------------------------------------------------------------------
# Test 3: production replicas >= 2 for every enabled appService
# ---------------------------------------------------------------------------
def test_production_replicas(results: list, prod: dict) -> None:
    app_services = prod.get("appServices") or {}
    failures = []
    for name, svc in app_services.items():
        if not (svc.get("enabled", True)):
            continue
        replicas = svc.get("replicas") or 0
        if replicas < 2:
            failures.append(f"{name}={replicas}")
    results.append(check(
        "production.replicas_ge_2_for_all_services",
        not failures,
        "all >= 2" if not failures else "below 2: " + ", ".join(failures),
    ))


# ---------------------------------------------------------------------------
# Test 4: production HPA minReplicas >= 3 for platform-api / gateway / web
# ---------------------------------------------------------------------------
def test_production_hpa_min(results: list, prod: dict) -> None:
    app_services = prod.get("appServices") or {}
    failures = []
    for name in STRICT_HPA_SERVICES:
        svc = app_services.get(name) or {}
        hpa = svc.get("hpa") or {}
        if not hpa.get("enabled"):
            failures.append(f"{name}.hpa.enabled=false")
            continue
        if (hpa.get("minReplicas") or 0) < 3:
            failures.append(f"{name}.hpa.minReplicas={hpa.get('minReplicas')}")
        if (hpa.get("maxReplicas") or 0) < (hpa.get("minReplicas") or 0):
            failures.append(f"{name}.hpa.maxReplicas={hpa.get('maxReplicas')} < min")
    results.append(check(
        "production.hpa_minReplicas_ge_3_for_stateless_services",
        not failures,
        "all >= 3" if not failures else "; ".join(failures),
    ))


# ---------------------------------------------------------------------------
# Test 5: production resources.limits defined for every enabled appService
# ---------------------------------------------------------------------------
def test_production_resources_limits(results: list, prod: dict) -> None:
    app_services = prod.get("appServices") or {}
    failures = []
    for name, svc in app_services.items():
        if not (svc.get("enabled", True)):
            continue
        res = svc.get("resources")
        if not has_limits(res):
            failures.append(name)
    results.append(check(
        "production.resources_limits_defined_for_all_services",
        not failures,
        "all have limits" if not failures else "missing limits: " + ", ".join(failures),
    ))


# ---------------------------------------------------------------------------
# Test 6: production affinity + topologySpreadConstraints present
# ---------------------------------------------------------------------------
def test_production_scheduling(results: list, prod: dict) -> None:
    app_services = prod.get("appServices") or {}
    has_affinity = bool(prod.get("affinity"))
    has_topo = bool(prod.get("topologySpreadConstraints"))
    # Per-service overrides count too - stateless services must define both.
    per_service_affinity = []
    per_service_topo = []
    for name in STRICT_HPA_SERVICES:
        svc = app_services.get(name) or {}
        if svc.get("affinity"):
            per_service_affinity.append(name)
        if svc.get("topologySpreadConstraints"):
            per_service_topo.append(name)
    affinity_ok = has_affinity or len(per_service_affinity) >= 1
    topo_ok = has_topo or len(per_service_topo) >= 1
    results.append(check(
        "production.affinity_present",
        affinity_ok,
        f"global={has_affinity} per_service={per_service_affinity}",
    ))
    results.append(check(
        "production.topologySpreadConstraints_present",
        topo_ok,
        f"global={has_topo} per_service={per_service_topo}",
    ))


# ---------------------------------------------------------------------------
# Test 7: production networkPolicy.enabled == true
# ---------------------------------------------------------------------------
def test_production_network_policy(results: list, prod: dict) -> None:
    np = prod.get("networkPolicy") or {}
    ok = np.get("enabled") is True
    results.append(check(
        "production.networkPolicy.enabled",
        ok,
        f"enabled={np.get('enabled')} ingressFromSameNamespace={np.get('ingressFromSameNamespace')}",
    ))


# ---------------------------------------------------------------------------
# Test 8: production storage values
# ---------------------------------------------------------------------------
def test_production_storage(results: list, prod: dict) -> None:
    pg = prod.get("postgres") or {}
    rd = prod.get("redis") or {}
    mn = prod.get("minio") or {}
    pg_ok = pg.get("storage") == "100Gi"
    rd_ok = rd.get("storage") == "20Gi"
    mn_ok = mn.get("storage") == "500Gi"
    results.append(check(
        "production.storage_values",
        pg_ok and rd_ok and mn_ok,
        f"postgres={pg.get('storage')} redis={rd.get('storage')} minio={mn.get('storage')}",
    ))


# ---------------------------------------------------------------------------
# Test 9: production tolerations defined
# ---------------------------------------------------------------------------
def test_production_tolerations(results: list, prod: dict) -> None:
    tolerations = prod.get("tolerations") or []
    ok = isinstance(tolerations, list) and len(tolerations) >= 1
    results.append(check(
        "production.tolerations_defined",
        ok,
        f"count={len(tolerations)}",
    ))


# ---------------------------------------------------------------------------
# Test 10: .env.production.template contains all required variables
# ---------------------------------------------------------------------------
def test_env_template_required_vars(results: list) -> None:
    if not ENV_TEMPLATE.exists():
        results.append(check(
            "env_production_template.required_vars_present",
            False,
            f"file missing: {ENV_TEMPLATE}",
        ))
        return
    text = read_text(ENV_TEMPLATE)
    missing = [var for var in REQUIRED_ENV_VARS if not re.search(rf"^{re.escape(var)}=", text, re.MULTILINE)]
    # Also verify at least one placeholder token is present (no real secrets committed).
    has_placeholder = "CHANGE_ME" in text
    results.append(check(
        "env_production_template.required_vars_present",
        not missing and has_placeholder,
        f"missing={missing or 'none'} placeholder_present={has_placeholder}",
    ))


# ---------------------------------------------------------------------------
# Test 11: docs/deployment-guide.md exists and has all required sections
# ---------------------------------------------------------------------------
def test_deployment_guide_sections(results: list) -> None:
    if not DEPLOYMENT_GUIDE.exists():
        results.append(check(
            "deployment_guide.has_all_sections",
            False,
            f"file missing: {DEPLOYMENT_GUIDE}",
        ))
        return
    text = read_text(DEPLOYMENT_GUIDE)
    missing = [s for s in REQUIRED_DEPLOYMENT_SECTIONS if s not in text]
    results.append(check(
        "deployment_guide.has_all_sections",
        not missing,
        f"missing={missing or 'none'}",
    ))


# ---------------------------------------------------------------------------
# Test 12: docs/docker-compose-production.md exists and has all required sections
# ---------------------------------------------------------------------------
def test_compose_doc_sections(results: list) -> None:
    if not COMPOSE_DOC.exists():
        results.append(check(
            "docker_compose_production.has_all_sections",
            False,
            f"file missing: {COMPOSE_DOC}",
        ))
        return
    text = read_text(COMPOSE_DOC)
    missing = [s for s in REQUIRED_COMPOSE_SECTIONS if s not in text]
    results.append(check(
        "docker_compose_production.has_all_sections",
        not missing,
        f"missing={missing or 'none'}",
    ))


# ---------------------------------------------------------------------------
# Test 13: staging lighter than production (replicas + HPA + storage)
# ---------------------------------------------------------------------------
def test_staging_lighter_than_production(results: list, staging: dict, prod: dict) -> None:
    failures = []
    # platform-api.replicas: staging 2, prod 3
    s_api = (staging.get("appServices") or {}).get("platform-api") or {}
    p_api = (prod.get("appServices") or {}).get("platform-api") or {}
    s_repl = s_api.get("replicas") or 0
    p_repl = p_api.get("replicas") or 0
    if not (2 <= s_repl <= p_repl):
        failures.append(f"platform-api.replicas staging={s_repl} prod={p_repl}")
    # platform-api.hpa: staging maxReplicas <= prod maxReplicas
    s_hpa = s_api.get("hpa") or {}
    p_hpa = p_api.get("hpa") or {}
    if (s_hpa.get("minReplicas") or 0) < 2:
        failures.append(f"platform-api.hpa.minReplicas={s_hpa.get('minReplicas')} < 2")
    if (s_hpa.get("maxReplicas") or 0) > (p_hpa.get("maxReplicas") or 0):
        failures.append(
            f"platform-api.hpa.maxReplicas staging={s_hpa.get('maxReplicas')} > prod={p_hpa.get('maxReplicas')}"
        )
    # Storage: staging < prod
    s_pg = (staging.get("postgres") or {}).get("storage") or "0"
    p_pg = (prod.get("postgres") or {}).get("storage") or "0"
    if _parse_gi(s_pg) >= _parse_gi(p_pg):
        failures.append(f"postgres.storage staging={s_pg} >= prod={p_pg}")
    s_rd = (staging.get("redis") or {}).get("storage") or "0"
    p_rd = (prod.get("redis") or {}).get("storage") or "0"
    if _parse_gi(s_rd) >= _parse_gi(p_rd):
        failures.append(f"redis.storage staging={s_rd} >= prod={p_rd}")
    results.append(check(
        "staging.lighter_than_production",
        not failures,
        "ok" if not failures else "; ".join(failures),
    ))


def _parse_gi(value: str) -> int:
    """Parse a Kubernetes storage string like '100Gi' / '1Ti' into Mi for compare."""
    if not value:
        return 0
    m = re.match(r"^(\d+)\s*([KMGT]i?)?B?$", str(value))
    if not m:
        return 0
    n = int(m.group(1))
    unit = m.group(2) or ""
    multipliers = {
        "": 1, "K": 1_000, "Ki": 1_024,
        "M": 1_000_000, "Mi": 1_024 ** 2,
        "G": 1_000_000_000, "Gi": 1_024 ** 3,
        "T": 1_000_000_000_000, "Ti": 1_024 ** 4,
    }
    return n * multipliers.get(unit, 1)


# ---------------------------------------------------------------------------
# Test 14: production values do NOT contain real secrets (only placeholders)
# ---------------------------------------------------------------------------
def test_production_no_real_secrets(results: list, prod: dict) -> None:
    secrets = prod.get("secrets") or {}
    placeholders_ok = all(
        (isinstance(v, str) and ("REPLACE" in v or "change" in v.lower() or v == ""))
        for v in secrets.values()
    )
    pg_pwd = (prod.get("postgres") or {}).get("password") or ""
    mn_ak = (prod.get("minio") or {}).get("accessKey") or ""
    mn_sk = (prod.get("minio") or {}).get("secretKey") or ""
    infra_ok = all(
        ("REPLACE" in v or "change" in v.lower() or v == "")
        for v in (pg_pwd, mn_ak, mn_sk)
    )
    results.append(check(
        "production.no_real_secrets_committed",
        placeholders_ok and infra_ok,
        f"secrets_placeholder={placeholders_ok} infra_placeholder={infra_ok}",
    ))


# ---------------------------------------------------------------------------
# Test 15: production imagePullSecrets references a named secret
# ---------------------------------------------------------------------------
def test_production_image_pull_secrets(results: list, prod: dict) -> None:
    ips = (prod.get("global") or {}).get("imagePullSecrets") or []
    ok = isinstance(ips, list) and len(ips) >= 1 and ips[0].get("name")
    results.append(check(
        "production.imagePullSecrets_named",
        ok,
        f"secrets={ips}",
    ))


def main() -> int:
    results: list = []

    # --- Sanity: default values.yaml + Chart.yaml still parse ---
    try:
        load_yaml(VALUES_DEFAULT)
        default_ok = True
    except Exception:
        default_ok = False
    results.append(check("values.yaml_still_parses", default_ok))
    try:
        chart = load_yaml(CHART_FILE)
        chart_ok = chart.get("apiVersion") == "v2" and chart.get("name") == "workama"
    except Exception:
        chart_ok = False
    results.append(check("Chart.yaml_still_parses", chart_ok))

    # --- Run all production / staging tests ---
    prod = test_values_production_yaml_syntax(results)
    staging = test_values_staging_yaml_syntax(results)

    if prod:
        test_production_replicas(results, prod)
        test_production_hpa_min(results, prod)
        test_production_resources_limits(results, prod)
        test_production_scheduling(results, prod)
        test_production_network_policy(results, prod)
        test_production_storage(results, prod)
        test_production_tolerations(results, prod)
        test_production_no_real_secrets(results, prod)
        test_production_image_pull_secrets(results, prod)
    if staging and prod:
        test_staging_lighter_than_production(results, staging, prod)

    test_env_template_required_vars(results)
    test_deployment_guide_sections(results)
    test_compose_doc_sections(results)

    # --- Tally and write evidence ---
    passed = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)
    ok = failed == 0

    evidence = {
        "ok": ok,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
        },
        "checks": [r.to_dict() for r in results],
        "failures": [r.to_dict() for r in results if not r.ok],
    }
    EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with EVIDENCE_FILE.open("w", encoding="utf-8") as fh:
        json.dump(evidence, fh, indent=2, ensure_ascii=False)

    print(f"Deployment config tests: {'OK' if ok else 'FAIL'} "
          f"({passed} passed, {failed} failed, {len(results)} total)")
    for r in results:
        if not r.ok:
            print(f"  FAIL {r.name}: {r.detail}")
    print(f"  evidence: {EVIDENCE_FILE.relative_to(REPO_ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
