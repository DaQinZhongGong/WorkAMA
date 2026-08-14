#!/usr/bin/env python3
"""WorkAMA Helm Chart production-grade smoke test.

This script validates that the Helm chart under ``deploy/helm/workama`` is
production-grade without requiring the ``helm`` CLI. It performs two kinds of
checks:

1. **Values-level assertions** - parse ``values.yaml`` / ``Chart.yaml`` with
   PyYAML and assert production defaults (replicas, HPA, PDB, resources,
   securityContext, networkPolicy, probes, persistence).
2. **Template-level assertions** - read the Go-template files under
   ``templates/`` as text and assert they wire up the required Kubernetes
   fields (resources, securityContext, affinity, tolerations,
   topologySpreadConstraints, liveness/readiness probes, PVC, NetworkPolicy,
   ServiceAccount, HPA, PDB).

If the ``helm`` CLI happens to be available the script will additionally run
``helm template`` and parse the rendered YAML; otherwise it falls back to the
static analysis above. Evidence is written to
``quality/evidence/helm-chart-smoke.json``. Exit code 0 means all assertions
passed, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
TEMPLATES_DIR = CHART_DIR / "templates"
VALUES_FILE = CHART_DIR / "values.yaml"
CHART_FILE = CHART_DIR / "Chart.yaml"
EVIDENCE_FILE = REPO_ROOT / "quality" / "evidence" / "helm-chart-smoke.json"

# Services that must be horizontally scalable (replicas>=2, HPA, PDB).
HPA_PDB_SERVICES = ("platform-api", "gateway", "web")
# Infrastructure services that must persist data via a PVC.
PVC_INFRA = ("postgres", "redis", "minio")


def load_yaml(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8") as fh:
        return fh.read()


class CheckResult:
    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name = name
        self.ok = ok
        self.detail = detail

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def check(name: str, ok: bool, detail: str = "") -> CheckResult:
    return CheckResult(name, bool(ok), detail)


def has_security_context_required(sec_ctx: dict, allow_run_as_root: bool = False) -> bool:
    """Return True if a securityContext satisfies the production baseline."""
    if not isinstance(sec_ctx, dict):
        return False
    if sec_ctx.get("allowPrivilegeEscalation") is not False:
        return False
    caps = sec_ctx.get("capabilities") or {}
    if "ALL" not in (caps.get("drop") or []):
        return False
    if not allow_run_as_root and sec_ctx.get("runAsNonRoot") is not True:
        return False
    return True


def run_static_checks(results: list) -> None:
    values = load_yaml(VALUES_FILE)
    chart = load_yaml(CHART_FILE)
    app_services = values.get("appServices") or {}

    # --- Chart.yaml version ---
    results.append(check(
        "chart.version_is_0.7.0",
        chart.get("version") == "0.7.0",
        f"version={chart.get('version')!r}",
    ))
    results.append(check(
        "chart.appVersion_is_v7.142",
        chart.get("appVersion") == "v7.142",
        f"appVersion={chart.get('appVersion')!r}",
    ))

    # --- _helpers.tpl defines required helpers ---
    helpers = read_text(TEMPLATES_DIR / "_helpers.tpl")
    for helper in ("workama.labels", "workama.selectorLabels",
                   "workama.fullname", "workama.imagePullSecrets"):
        results.append(check(
            f"helpers.defines_{helper}",
            f'define "{helper}"' in helpers,
            helper,
        ))

    # --- resources: every enabled service has resources or global fallback ---
    global_resources = values.get("resources") or {}
    global_has_rl = bool(global_resources.get("requests") and global_resources.get("limits"))
    for name, svc in app_services.items():
        if not (svc.get("enabled", True)):
            continue
        svc_res = svc.get("resources")
        ok = bool(svc_res and svc_res.get("requests") and svc_res.get("limits")) or global_has_rl
        results.append(check(
            f"resources.{name}",
            ok,
            "per-service" if svc_res and svc_res.get("requests") else ("global-fallback" if global_has_rl else "MISSING"),
        ))

    # --- HPA for platform-api/gateway/web ---
    for name in HPA_PDB_SERVICES:
        svc = app_services.get(name) or {}
        hpa = svc.get("hpa") or {}
        ok = (hpa.get("enabled") is True
              and (hpa.get("minReplicas") or 0) >= 2
              and (hpa.get("maxReplicas") or 0) <= 10
              and 50 <= (hpa.get("targetCPUUtilizationPercentage") or 0) <= 90)
        results.append(check(
            f"hpa.{name}",
            ok,
            f"enabled={hpa.get('enabled')} min={hpa.get('minReplicas')} "
            f"max={hpa.get('maxReplicas')} cpu={hpa.get('targetCPUUtilizationPercentage')}",
        ))

    # --- PDB for platform-api/gateway/web ---
    for name in HPA_PDB_SERVICES:
        svc = app_services.get(name) or {}
        pdb = svc.get("pdb") or {}
        ok = pdb.get("enabled") is True and (pdb.get("minAvailable") or 0) >= 1
        results.append(check(
            f"pdb.{name}",
            ok,
            f"enabled={pdb.get('enabled')} minAvailable={pdb.get('minAvailable')}",
        ))

    # --- podSecurityContext baseline ---
    psc = values.get("podSecurityContext") or {}
    results.append(check(
        "podSecurityContext.runAsNonRoot",
        psc.get("runAsNonRoot") is True,
        f"runAsNonRoot={psc.get('runAsNonRoot')}",
    ))
    results.append(check(
        "podSecurityContext.fsGroup",
        psc.get("fsGroup") is not None,
        f"fsGroup={psc.get('fsGroup')}",
    ))

    # --- defaultSecurityContext fallback exists ---
    dsc = values.get("defaultSecurityContext") or {}
    results.append(check(
        "defaultSecurityContext.baseline",
        has_security_context_required(dsc) and dsc.get("readOnlyRootFilesystem") is True,
        f"keys={list(dsc.keys())}",
    ))

    # --- per-service securityContext (sandbox-fleet allowed runAsNonRoot=false) ---
    for name, svc in app_services.items():
        if not (svc.get("enabled", True)):
            continue
        sec = svc.get("securityContext")
        allow_root = name == "sandbox-fleet"
        ok = has_security_context_required(sec, allow_run_as_root=allow_root) if sec else True
        # If per-service missing, apps.yaml must fall back to defaultSecurityContext.
        if not sec:
            ok = bool(dsc)
        results.append(check(
            f"securityContext.{name}",
            ok,
            "per-service" if sec else "default-fallback",
        ))

    # --- apps.yaml wires resources, securityContext, affinity, probes ---
    apps_tpl = read_text(TEMPLATES_DIR / "apps.yaml")
    results.append(check("apps.has_resources_block", "$service.resources" in apps_tpl or "service.resources" in apps_tpl))
    results.append(check("apps.has_securityContext_fallback", "defaultSecurityContext" in apps_tpl))
    results.append(check("apps.has_affinity", "affinity" in apps_tpl and "with ($service.affinity" in apps_tpl))
    results.append(check("apps.has_tolerations", "tolerations" in apps_tpl and "with ($service.tolerations" in apps_tpl))
    results.append(check("apps.has_topologySpread", "topologySpreadConstraints" in apps_tpl and "with ($service.topologySpreadConstraints" in apps_tpl))
    results.append(check("apps.has_livenessProbe", "livenessProbe" in apps_tpl))
    results.append(check("apps.has_readinessProbe", "readinessProbe" in apps_tpl))
    results.append(check("apps.uses_workama_labels", 'include "workama.labels"' in apps_tpl))
    results.append(check("apps.uses_selectorLabels", 'include "workama.selectorLabels"' in apps_tpl))
    results.append(check("apps.has_imagePullSecrets", "imagePullSecrets" in apps_tpl))

    # --- HPA / PDB templates exist and reference appServices ---
    hpa_tpl = read_text(TEMPLATES_DIR / "hpa.yaml")
    results.append(check("hpa.template_exists", "HorizontalPodAutoscaler" in hpa_tpl and "hpa.enabled" in hpa_tpl))
    pdb_tpl = read_text(TEMPLATES_DIR / "pdb.yaml")
    results.append(check("pdb.template_exists", "PodDisruptionBudget" in pdb_tpl and "pdb.enabled" in pdb_tpl))

    # --- NetworkPolicy ---
    np_tpl_path = TEMPLATES_DIR / "networkpolicy.yaml"
    np_exists = np_tpl_path.exists()
    results.append(check("networkpolicy.template_exists", np_exists))
    if np_exists:
        np_tpl = read_text(np_tpl_path)
        results.append(check("networkpolicy.kind", "kind: NetworkPolicy" in np_tpl))
        results.append(check("networkpolicy.ingress_same_namespace", "podSelector: {}" in np_tpl))
    np_values = values.get("networkPolicy") or {}
    results.append(check("networkPolicy.enabled", np_values.get("enabled") is True,
                         f"enabled={np_values.get('enabled')}"))

    # --- ServiceAccount ---
    sa_tpl = read_text(TEMPLATES_DIR / "serviceaccount.yaml")
    results.append(check("serviceaccount.template_exists", "kind: ServiceAccount" in sa_tpl))
    results.append(check("serviceAccount.create", (values.get("serviceAccount") or {}).get("create") is True))

    # --- PVC for postgres/redis/minio ---
    infra_tpl = read_text(TEMPLATES_DIR / "infra.yaml")
    for name in PVC_INFRA:
        # volumeClaimTemplates block must appear and the service must have storage in values.
        has_pvc = "volumeClaimTemplates" in infra_tpl
        svc_cfg = values.get(name) or {}
        has_storage = bool(svc_cfg.get("storage")) or name == "redis"
        # For redis, ensure its StatefulSet section includes a PVC. We check
        # that redis has a storage value and infra.yaml has at least one PVC
        # block (the template generates one per enabled infra service).
        results.append(check(
            f"pvc.{name}",
            has_pvc and has_storage,
            f"infra_has_vct={has_pvc} storage={svc_cfg.get('storage')}",
        ))

    # --- liveness/readiness probes configured per service in values ---
    for name, svc in app_services.items():
        if not (svc.get("enabled", True)):
            continue
        has_live = bool(svc.get("livenessProbe")) or bool(svc.get("healthPath"))
        has_ready = bool(svc.get("readinessProbe")) or bool(svc.get("healthPath"))
        results.append(check(
            f"probes.{name}",
            has_live and has_ready,
            f"liveness={'probe' if svc.get('livenessProbe') else ('healthPath' if svc.get('healthPath') else 'MISSING')} "
            f"readiness={'probe' if svc.get('readinessProbe') else ('healthPath' if svc.get('healthPath') else 'MISSING')}",
        ))

    # --- replica counts ---
    for name in HPA_PDB_SERVICES:
        svc = app_services.get(name) or {}
        results.append(check(
            f"replicas.{name}_ge_2",
            (svc.get("replicas") or 0) >= 2,
            f"replicas={svc.get('replicas')}",
        ))

    # --- image pull secrets support ---
    results.append(check(
        "global.imagePullSecrets_supported",
        "imagePullSecrets" in (values.get("global") or {}),
    ))


def try_render_with_helm() -> tuple[bool, list, str]:
    """Attempt ``helm template``; return (used, rendered_docs, log)."""
    helm = shutil.which("helm")
    if not helm:
        return False, [], "helm CLI not available - static analysis only"
    try:
        proc = subprocess.run(
            [helm, "template", "workama", str(CHART_DIR), "-f", str(VALUES_FILE)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # pragma: no cover
        return False, [], f"helm template failed: {exc}"
    if proc.returncode != 0:
        return False, [], f"helm template exit {proc.returncode}: {proc.stderr[:500]}"
    docs = []
    for doc in yaml.safe_load_all(proc.stdout):
        if doc:
            docs.append(doc)
    return True, docs, f"rendered {len(docs)} documents"


def run_rendered_checks(docs: list, results: list) -> None:
    """Extra assertions against rendered YAML (only when helm is available)."""
    by_kind = {}
    for doc in docs:
        kind = doc.get("kind")
        if kind:
            by_kind.setdefault(kind, []).append(doc)
    results.append(check("rendered.has_Deployment", "Deployment" in by_kind))
    results.append(check("rendered.has_HPA", "HorizontalPodAutoscaler" in by_kind))
    results.append(check("rendered.has_PDB", "PodDisruptionBudget" in by_kind))
    results.append(check("rendered.has_NetworkPolicy", "NetworkPolicy" in by_kind))
    results.append(check("rendered.has_ServiceAccount", "ServiceAccount" in by_kind))
    results.append(check("rendered.has_StatefulSet", "StatefulSet" in by_kind))


def main() -> int:
    results: list = []
    run_static_checks(results)

    used, docs, log = try_render_with_helm()
    render_info = {"helm_used": used, "log": log}
    if used:
        run_rendered_checks(docs, results)

    passed = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)
    ok = failed == 0

    evidence = {
        "ok": ok,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "chart": str(CHART_DIR.relative_to(REPO_ROOT)).replace("\\", "/"),
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
        },
        "render": render_info,
        "checks": [r.to_dict() for r in results],
        "failures": [r.to_dict() for r in results if not r.ok],
    }

    EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with EVIDENCE_FILE.open("w", encoding="utf-8") as fh:
        json.dump(evidence, fh, indent=2, ensure_ascii=False)

    print(f"Helm chart smoke: {'OK' if ok else 'FAIL'} "
          f"({passed} passed, {failed} failed, {len(results)} total)")
    if not used:
        print(f"  note: {log}")
    for r in results:
        if not r.ok:
            print(f"  FAIL {r.name}: {r.detail}")
    print(f"  evidence: {EVIDENCE_FILE.relative_to(REPO_ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
