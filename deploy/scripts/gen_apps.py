#!/usr/bin/env python3
"""Generate the per-service Kubernetes manifests of Burn Rate from the code. Run it through gen-apps.sh.

What is read (never imported, only parsed, so no dependency has to be installed):
  burnrate/services/__init__.py   SERVICES, WORKERS, LOOPS, JOBS  -> one Deployment / CronJob / Job each
  burnrate/**/*.py                @subscribe("topic", worker="x")  -> KEDA triggers of worker x
                                  emit(s, SVC, "topic", ...)       -> topics the code publishes
                                  make_base("name")                -> PostgreSQL databases
  deploy/apps/topics.txt          the declared Kafka topics (KafkaTopic objects of the `full` profile)
  deploy/apps/worker-topics.txt   optional manual worker -> topics lines (win over the code)

What is written (every file starts with a "generated" line; hand edits are lost on the next run):
  deploy/apps/small/**  deploy/apps/full/**   kustomizations of the two profiles
  deploy/apps/jobs/schema.yaml                Job template run by deploy.sh before a rollout
  deploy/platform/cnpg/databases.yaml         one Database per service
  deploy/platform/strimzi/topics.yaml         KafkaTopic objects (applied in the `full` profile)
  deploy/compose/initdb.sh                    the same databases for docker-compose.yml
  deploy/apps/GENERATED.txt                   what was found: counts, worker -> topics, warnings

Adding a service = one line in burnrate/services/__init__.py, then deploy/scripts/gen-apps.sh.
Sizing lives in PROFILES / RESOURCES below.

Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPS = ROOT / "deploy" / "apps"
PLATFORM = ROOT / "deploy" / "platform"

NAMESPACE = "burnrate"
DATA_NS = "data"
KAFKA_CLUSTER = "burnrate"
KAFKA_BOOTSTRAP = f"{KAFKA_CLUSTER}-kafka-bootstrap.{DATA_NS}.svc.cluster.local:9092"
PG_CLUSTER = "pg"
PG_ROLE = "burnrate"
CONFIGMAP = "burnrate-config"
SECRET = "burnrate-secrets"
API_UID = 10001  # Dockerfile
WEB_UID = 101  # nginxinc/nginx-unprivileged

# ------------------------------------------------------------------------------------------------------------
# Sizing. cpu request, memory request, cpu limit, memory limit.
# ------------------------------------------------------------------------------------------------------------
RESOURCES = {
    "default": ("100m", "128Mi", "500m", "384Mi"),
    "engine": ("200m", "256Mi", "1", "512Mi"),
    "statement-extractor": ("200m", "256Mi", "1", "512Mi"),
    "web": ("25m", "32Mi", "200m", "96Mi"),
    "job": ("100m", "128Mi", "500m", "384Mi"),
}
# Server-sent events (the rehearsal chat) pass through these: give open streams time to finish on shutdown.
STREAMING = {"edge-api", "rehearsal", "llm-gateway"}
HPA_CPU_PERCENT = 70
LAG_THRESHOLD = "50"  # messages of lag per replica before KEDA adds one

PROFILES = {
    # 2 nodes x 4 vCPU / 8 GB (about 12.5 GiB allocatable). edge-api, web and engine twice, everything else once.
    # Autoscaling ceilings are low on purpose: there is no room for more (deploy/README.md, memory budget).
    "small": {
        "replicas": {"edge-api": 2, "engine": 2, "web": 2},
        "default_replicas": 1,
        "hpa": {"edge-api": (2, 3), "engine": (2, 3), "funnel": (1, 2), "web": (2, 3)},
        "worker_min": 1,
        "worker_max": 2,
        # Two nodes have 8 vCPU between them and 30 Deployments want a share: an idle service reserves 50m
        # instead of 100m. The autoscaled services keep 100m/200m (their CPU target is a percentage of it).
        "idle_cpu_request": "50m",
        "hard_spread": False,
        "config_patch": {},
        # TLS ends at the Caddy server in front of the cluster; the gateway serves plain HTTP on its port 80 listener
        "route_listener": "http",
    },
    # after the account limit is raised: two or more of everything, TLS at the gateway
    "full": {
        "replicas": {"edge-api": 3, "engine": 3, "web": 3},
        "default_replicas": 2,
        "hpa": {"edge-api": (3, 8), "engine": (3, 8), "funnel": (2, 6), "web": (3, 6)},
        "worker_min": 2,
        "worker_max": 6,  # topics are declared with 6 partitions in this profile
        "idle_cpu_request": "100m",
        "hard_spread": True,
        # Tempo exists only in this profile. The app has no OpenTelemetry SDK yet; the variable is ready for it.
        "config_patch": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://tempo.observability.svc.cluster.local:4318"},
        "route_listener": "https",
    },
}
TOPIC_PARTITIONS_FULL = 6
TOPIC_REPLICAS_FULL = 3
TOPIC_RETENTION_MS = 14 * 24 * 3600 * 1000  # architecture section 5: Kafka keeps 14 days


# ------------------------------------------------------------------------------------------------------------
# A small YAML writer (the standard library has none). Emits block style; strings are quoted when needed.
# ------------------------------------------------------------------------------------------------------------
_PLAIN = re.compile(r"^[A-Za-z_/][A-Za-z0-9_./-]*$")
_WORDS = {"true", "false", "null", "yes", "no", "on", "off", "y", "n", "~"}


def _scalar(v) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if _PLAIN.match(s) and s.lower() not in _WORDS:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lines(obj, ind: int) -> list[str]:
    pad = " " * ind
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = _scalar(k)
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{key}:")
                out += _lines(v, ind + 2)
            elif isinstance(v, dict):
                out.append(f"{pad}{key}: {{}}")
            elif isinstance(v, list):
                out.append(f"{pad}{key}: []")
            elif isinstance(v, str) and "\n" in v:  # block scalar
                out.append(f"{pad}{key}: |" + ("" if v.endswith("\n") else "-"))
                out += [(" " * (ind + 2) + ln) if ln else "" for ln in v.rstrip("\n").split("\n")]
            else:
                out.append(f"{pad}{key}: {_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)) and item:
                sub = _lines(item, ind + 2)
                sub[0] = f"{pad}- {sub[0].lstrip()}"
                out += sub
            elif isinstance(item, dict):
                out.append(f"{pad}- {{}}")
            elif isinstance(item, list):
                out.append(f"{pad}- []")
            else:
                out.append(f"{pad}- {_scalar(item)}")
    else:
        out.append(f"{pad}{_scalar(obj)}")
    return out


def dump(*docs, header: str = "") -> str:
    parts = ["\n".join(_lines(d, 0)) for d in docs if d]
    head = "".join(f"# {line}\n" if line else "#\n" for line in header.splitlines()) if header else ""
    return head + "\n---\n".join(parts) + "\n"


# ------------------------------------------------------------------------------------------------------------
# Reading the code
# ------------------------------------------------------------------------------------------------------------
class Code:
    def __init__(self) -> None:
        self.services: list[str] = []
        self.workers: list[str] = []
        self.loops: list[str] = []
        self.jobs: list[str] = []
        self.subs: dict[str, set[str]] = {}  # worker -> topics
        self.sub_where: dict[tuple[str, str], str] = {}
        self.emitted: dict[str, str] = {}  # topic -> first place it is published
        self.databases: dict[str, str] = {}  # database -> where make_base() names it
        self.warnings: list[str] = []


def _str(node, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _callee(node: ast.Call) -> str:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def read_registry(code: Code) -> None:
    path = ROOT / "burnrate" / "services" / "__init__.py"
    tree = ast.parse(path.read_text())
    found: dict[str, list[str]] = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target not in ("SERVICES", "WORKERS", "LOOPS", "JOBS") or value is None:
            continue
        if isinstance(value, ast.Dict):
            found[target] = [k.value for k in value.keys if isinstance(k, ast.Constant)]
        elif isinstance(value, (ast.Tuple, ast.List)):
            found[target] = [e.value for e in value.elts if isinstance(e, ast.Constant)]
    for name in ("SERVICES", "WORKERS", "LOOPS", "JOBS"):
        if name not in found:
            sys.exit(f"gen-apps: {name} not found in {path.relative_to(ROOT)}")
    code.services, code.workers, code.loops, code.jobs = (found[n] for n in ("SERVICES", "WORKERS", "LOOPS", "JOBS"))
    names = code.services + code.workers + code.loops + code.jobs + ["web"]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        sys.exit(f"gen-apps: names used twice across SERVICES/WORKERS/LOOPS/JOBS/web: {dupes}")
    for n in names:
        if not re.fullmatch(r"[a-z]([a-z0-9-]{0,50}[a-z0-9])?", n):
            sys.exit(f"gen-apps: {n!r} is not a valid Kubernetes name")


def scan_code(code: Code) -> None:
    for path in sorted((ROOT / "burnrate").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as e:  # a file being written right now
            code.warnings.append(f"{rel}: not parsed ({e.msg}, line {e.lineno}); its topics and databases are missing")
            continue
        consts: dict[str, str] = {}
        tables: dict[str, list[str]] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name, value = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                name, value = node.target.id, node.value
            else:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                consts[name] = value.value
            elif isinstance(value, ast.Dict):
                tables[name] = [v.value for v in value.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            elif isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                tables[name] = [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn, where = _callee(node), f"{rel}:{node.lineno}"
            if fn == "subscribe":
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                if "worker" not in kw:
                    continue  # some other subscribe()
                topic = _str(node.args[0], consts) if node.args else _str(kw.get("topic"), consts)
                worker = _str(kw["worker"], consts)
                if topic is None or worker is None:
                    code.warnings.append(f"{where}: subscribe() with a topic or worker that is not a string literal; "
                                         f"add it to deploy/apps/worker-topics.txt by hand")
                    continue
                code.subs.setdefault(worker, set()).add(topic)
                code.sub_where.setdefault((worker, topic), where)
            elif fn == "emit" and len(node.args) >= 3:
                arg = node.args[2]
                topic = _str(arg, consts)
                if topic is not None:
                    code.emitted.setdefault(topic, where)
                elif isinstance(arg, ast.Subscript) and isinstance(arg.value, ast.Name) and arg.value.id in tables:
                    for t in tables[arg.value.id]:  # emit(s, SVC, TOPICS[kind], ...)
                        code.emitted.setdefault(t, where)
                else:
                    code.warnings.append(f"{where}: emit() with a topic that is not a string literal; "
                                         f"make sure it is in deploy/apps/topics.txt")
            elif fn == "make_base" and node.args:
                name = _str(node.args[0], consts)
                if name is None:
                    code.warnings.append(f"{where}: make_base() with a name that is not a string literal")
                else:
                    code.databases.setdefault(name.replace("-", "_"), where)


def read_table(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.split("#", 1)[0].strip() for ln in path.read_text().splitlines() if ln.split("#", 1)[0].strip()]


# ------------------------------------------------------------------------------------------------------------
# Building blocks
# ------------------------------------------------------------------------------------------------------------
def labels(name: str, component: str) -> dict:
    return {"app.kubernetes.io/name": name, "app.kubernetes.io/component": component}


def res(key: str, idle_cpu: str | None = None) -> dict:
    """Requests and limits of a workload. idle_cpu replaces the CPU request of workloads sized by "default"."""
    cpu, mem, cpu_l, mem_l = RESOURCES.get(key, RESOURCES["default"])
    if idle_cpu and key not in RESOURCES:
        cpu = idle_cpu
    return {"requests": {"cpu": cpu, "memory": mem}, "limits": {"cpu": cpu_l, "memory": mem_l}}


CONTAINER_SECURITY = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}


def pod_security(uid: int) -> dict:
    return {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid, "fsGroup": uid,
            "seccompProfile": {"type": "RuntimeDefault"}}


def spread(name: str, hard: bool) -> list:
    c = {"maxSkew": 1, "topologyKey": "kubernetes.io/hostname",
         "whenUnsatisfiable": "DoNotSchedule" if hard else "ScheduleAnyway",
         "labelSelector": {"matchLabels": {"app.kubernetes.io/name": name}}}
    if hard:
        c["nodeTaintsPolicy"] = "Honor"  # a cordoned node must not count as an empty domain during a drain
    return [c]


def api_container(name: str, image: str, *, http: bool, idle_cpu: str | None = None) -> dict:
    c: dict = {
        "name": "app",
        "image": image,
        "imagePullPolicy": "Always",
    }
    if http:
        c["ports"] = [{"name": "http", "containerPort": 8000}]
    c["env"] = [{"name": "SERVICE", "value": name}, {"name": "OTEL_SERVICE_NAME", "value": name}]
    c["envFrom"] = [{"configMapRef": {"name": CONFIGMAP}}, {"secretRef": {"name": SECRET}}]
    if http:
        c["startupProbe"] = {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 3, "failureThreshold": 40}
        c["readinessProbe"] = {"httpGet": {"path": "/readyz", "port": "http"}, "periodSeconds": 10, "timeoutSeconds": 3,
                               "failureThreshold": 3}
        c["livenessProbe"] = {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 20, "timeoutSeconds": 3,
                              "failureThreshold": 3}
        # keep serving while the endpoint is being removed from the Service, then uvicorn drains on SIGTERM
        c["lifecycle"] = {"preStop": {"sleep": {"seconds": 5}}}
    c["resources"] = res(name, idle_cpu)
    c["securityContext"] = dict(CONTAINER_SECURITY)
    # read-only root filesystem: uploads are spooled to /tmp, BR_DATA_DIR=/data
    c["volumeMounts"] = [{"name": "tmp", "mountPath": "/tmp"}, {"name": "data", "mountPath": "/data"}]
    return c


API_VOLUMES = [{"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}, {"name": "data", "emptyDir": {"sizeLimit": "64Mi"}}]


def api_pod(name: str, image: str, pull_secret: str, *, http: bool, grace: int, spread_hard: bool | None,
            restart: str | None = None, idle_cpu: str | None = None) -> dict:
    spec: dict = {}
    if restart:
        spec["restartPolicy"] = restart
    spec["imagePullSecrets"] = [{"name": pull_secret}]
    spec["enableServiceLinks"] = False  # 18 Services would add ~130 *_PORT_* variables to every container
    spec["terminationGracePeriodSeconds"] = grace
    spec["securityContext"] = pod_security(API_UID)
    if spread_hard is not None:
        spec["topologySpreadConstraints"] = spread(name, spread_hard)
    spec["containers"] = [api_container(name, image, http=http, idle_cpu=idle_cpu)]
    spec["volumes"] = API_VOLUMES
    return spec


def deployment(name: str, component: str, pod: dict, *, replicas: int | None, strategy: dict,
               min_ready: int = 0, pod_annotations: dict | None = None) -> dict:
    spec: dict = {}
    if replicas is not None:
        spec["replicas"] = replicas
    spec["revisionHistoryLimit"] = 3
    if min_ready:
        spec["minReadySeconds"] = min_ready
    spec["strategy"] = strategy
    spec["selector"] = {"matchLabels": {"app.kubernetes.io/name": name}}
    meta: dict = {"labels": {**labels(name, component), "app.kubernetes.io/part-of": "burnrate"}}
    if pod_annotations:
        meta["annotations"] = pod_annotations
    spec["template"] = {"metadata": meta, "spec": pod}
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "labels": labels(name, component)}, "spec": spec}


ROLLING = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}
# Queue consumers: stop the old pod first. Nobody waits for a worker, the messages wait in Kafka, and a rollout
# then needs no spare memory for a second copy (one consumer-group rebalance instead of two as well).
ROLLING_NO_SURGE = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}}


def service(name: str, component: str, port: int) -> dict:
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "labels": labels(name, component)},
            "spec": {"type": "ClusterIP", "selector": {"app.kubernetes.io/name": name},
                     "ports": [{"name": "http", "port": port, "targetPort": "http", "appProtocol": "http"}]}}


def pdb(name: str, component: str) -> dict:
    return {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
            "metadata": {"name": name, "labels": labels(name, component)},
            "spec": {"maxUnavailable": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": name}}}}


def hpa(name: str, component: str, lo: int, hi: int) -> dict:
    return {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": name, "labels": labels(name, component)},
            "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": name},
                     "minReplicas": lo, "maxReplicas": hi,
                     # the app container only: a Linkerd proxy in the pod must not dilute the percentage
                     "metrics": [{"type": "ContainerResource",
                                  "containerResource": {"name": "cpu", "container": "app",
                                                        "target": {"type": "Utilization",
                                                                   "averageUtilization": HPA_CPU_PERCENT}}}],
                     "behavior": {"scaleDown": {"stabilizationWindowSeconds": 300}}}}


def scaled_object(worker: str, topics: list[str], lo: int, hi: int) -> dict:
    def trigger(topic: str | None) -> dict:
        md = {"bootstrapServers": KAFKA_BOOTSTRAP, "consumerGroup": worker}
        if topic:
            md["topic"] = topic
        md["lagThreshold"] = LAG_THRESHOLD
        md["offsetResetPolicy"] = "earliest"  # same as the consumer (auto_offset_reset="earliest" in bus.py)
        return {"type": "kafka", "metadata": md}

    # No topic known for this worker: one trigger without `topic` (KEDA then adds up the lag of every topic the
    # consumer group has offsets for).
    return {"apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject",
            "metadata": {"name": worker, "labels": labels(worker, "worker")},
            "spec": {"scaleTargetRef": {"name": worker}, "minReplicaCount": lo, "maxReplicaCount": hi,
                     "pollingInterval": 30, "cooldownPeriod": 300,
                     "triggers": [trigger(t) for t in topics] or [trigger(None)]}}


NO_MESH = {"linkerd.io/inject": "disabled"}


# ------------------------------------------------------------------------------------------------------------
# One profile
# ------------------------------------------------------------------------------------------------------------
def gen_profile(profile: str, code: Code, worker_topics: dict[str, list[str]], out: Path, registry: str,
                pull_secret: str) -> dict:
    p = PROFILES[profile]
    api = f"{registry}/api:latest"
    web = f"{registry}/web:latest"
    head = f"GENERATED by deploy/scripts/gen-apps.sh (profile {profile}). Do not edit: change gen_apps.py and re-run."
    files: list[str] = []
    stats = {"deployments": 0, "pods": 0, "cpu_m": 0, "mem_mi": 0, "meshed_pods": 0}

    def idle(name: str) -> str | None:
        return None if name in p["hpa"] else p["idle_cpu_request"]

    def count(key: str, n: int, meshed: bool = True) -> None:
        r = res(key, idle(key))["requests"]
        stats["deployments"] += 1
        stats["pods"] += n
        stats["cpu_m"] += n * int(r["cpu"].rstrip("m"))
        stats["mem_mi"] += n * int(r["memory"].rstrip("Mi"))
        stats["meshed_pods"] += n if meshed else 0

    def write(rel: str, text: str) -> None:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if rel != "kustomization.yaml":
            files.append(rel)

    autoscaling: list[dict] = []

    # ---- online services: Deployment + Service (+ PodDisruptionBudget, HorizontalPodAutoscaler) ----
    for s in code.services:
        n = p["replicas"].get(s, p["default_replicas"])
        scaled = s in p["hpa"]
        lo, hi = p["hpa"].get(s, (n, n))
        pod = api_pod(s, api, pull_secret, http=True, grace=75 if s in STREAMING else 30,
                      spread_hard=p["hard_spread"] if (hi >= 2) else None, idle_cpu=idle(s))
        # replicas is left out when an autoscaler owns it: `kubectl apply` must not reset a scaled-out Deployment
        docs = [deployment(s, "service", pod, replicas=None if scaled else n, strategy=ROLLING),
                service(s, "service", 8000)]
        if lo >= 2:
            docs.append(pdb(s, "service"))
        if scaled:
            autoscaling.append(hpa(s, "service", lo, hi))
        write(f"services/{s}.yaml", dump(*docs, header=f"{head}\nOnline service \"{s}\": SERVICE={s}, port 8000."))
        count(s, lo)

    # ---- workers: Deployment, no Service, no probes; scaled by KEDA on consumer lag ----
    for w in code.workers:
        topics = worker_topics.get(w, [])
        lo, hi = p["worker_min"], p["worker_max"]
        pod = api_pod(w, api, pull_secret, http=False, grace=30, spread_hard=p["hard_spread"], idle_cpu=idle(w))
        docs = [deployment(w, "worker", pod, replicas=None, strategy=ROLLING_NO_SURGE, min_ready=20)]
        if lo >= 2:
            docs.append(pdb(w, "worker"))
        autoscaling.append(scaled_object(w, topics, lo, hi))
        note = ("topics: " + ", ".join(topics)) if topics else "NO TOPICS FOUND IN THE CODE (see deploy/apps/GENERATED.txt)"
        write(f"workers/{w}.yaml", dump(*docs, header=(
            f"{head}\nWorker \"{w}\": Kafka consumer, consumer group \"{w}\"; {note}.\n"
            "No HTTP server, so no probes: a crash restarts the container, a stuck consumer shows up as\n"
            "consumer lag (alert BurnRateConsumerLagGrowing). minReadySeconds keeps a rollout from\n"
            "reporting success for a pod that dies in its first seconds.")))
        count(w, lo)

    # ---- loops: exactly one replica, never two at once ----
    for lp in code.loops:
        pod = api_pod(lp, api, pull_secret, http=False, grace=30, spread_hard=None, idle_cpu=idle(lp))
        write(f"loops/{lp}.yaml", dump(
            deployment(lp, "loop", pod, replicas=1, strategy={"type": "Recreate"}, min_ready=20),
            header=f"{head}\nLoop \"{lp}\": a single replica in every profile (strategy Recreate: old pod stops before the\n"
                   "new one starts). No HTTP server, so no probes (same reasoning as for the workers)."))
        count(lp, 1)

    # ---- retention: daily CronJob ----
    if "retention" in code.jobs:
        pod = api_pod("retention", api, pull_secret, http=False, grace=30, spread_hard=None, restart="Never")
        pod["containers"][0]["resources"] = res("job")
        write("cronjobs/retention.yaml", dump({
            "apiVersion": "batch/v1", "kind": "CronJob",
            "metadata": {"name": "retention", "labels": labels("retention", "job")},
            "spec": {"schedule": "17 3 * * *", "timeZone": "America/New_York", "concurrencyPolicy": "Forbid",
                     "startingDeadlineSeconds": 3600, "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 3,
                     "jobTemplate": {"spec": {
                         "backoffLimit": 2, "activeDeadlineSeconds": 900, "ttlSecondsAfterFinished": 86400,
                         "template": {"metadata": {"labels": {**labels("retention", "job"),
                                                              "app.kubernetes.io/part-of": "burnrate"},
                                                   "annotations": NO_MESH},
                                      "spec": pod}}}}},
            header=f"{head}\nSERVICE=retention every night at 03:17 New York time: deletes stored statement files."))

    # ---- web: nginx with the built front end ----
    n = p["replicas"]["web"]
    lo, hi = p["hpa"].get("web", (n, n))
    web_pod = {
        "imagePullSecrets": [{"name": pull_secret}],
        "enableServiceLinks": False,
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 20,
        "securityContext": pod_security(WEB_UID),
        "topologySpreadConstraints": spread("web", p["hard_spread"]),
        "containers": [{
            "name": "app", "image": web, "imagePullPolicy": "Always",
            "ports": [{"name": "http", "containerPort": 8080}],
            "startupProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 2, "failureThreshold": 30},
            "readinessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 10, "timeoutSeconds": 2},
            "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 20, "timeoutSeconds": 2},
            "lifecycle": {"preStop": {"sleep": {"seconds": 5}}},
            "resources": res("web"),
            "securityContext": dict(CONTAINER_SECURITY),
            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
        }],
        "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "32Mi"}}],
    }
    docs = [deployment("web", "web", web_pod, replicas=None if "web" in p["hpa"] else n, strategy=ROLLING,
                       pod_annotations=NO_MESH),
            service("web", "web", 8080)]
    if lo >= 2:
        docs.append(pdb("web", "web"))
    if "web" in p["hpa"]:
        autoscaling.append(hpa("web", "web", lo, hi))
    write("web.yaml", dump(*docs, header=f"{head}\nFront end: nginx on :8080 (web/Dockerfile). Static files only, so it stays out of the mesh."))
    count("web", lo, meshed=False)

    write("autoscaling.yaml", dump(*autoscaling, header=(
        f"{head}\nHorizontalPodAutoscaler (CPU {HPA_CPU_PERCENT}% of the request) for {', '.join(p['hpa'])}.\n"
        f"KEDA ScaledObject on Kafka consumer lag for every worker: consumer group = worker name,\n"
        f"one trigger per topic, +1 replica per {LAG_THRESHOLD} messages of lag, {p['worker_min']}..{p['worker_max']} replicas.")))

    kust: dict = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
        "namespace": NAMESPACE,
        "labels": [{"pairs": {"app.kubernetes.io/part-of": "burnrate"}, "includeSelectors": False}],
        "resources": ["../common"] + files,
        # hostname, image tags and the config hash of this deployment (written by deploy.sh)
        "components": ["../env"],
    }
    patches = []
    if p["config_patch"]:
        patch = dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": CONFIGMAP}, "data": p["config_patch"]})
        patches.append({"patch": patch.rstrip("\n")})
    if p["route_listener"] != "http":  # deploy/apps/common/routes.yaml attaches to the listener "http"
        patches.append({"target": {"kind": "HTTPRoute", "name": "burnrate"},
                        "patch": "- op: replace\n  path: /spec/parentRefs/0/sectionName\n  value: " + p["route_listener"]})
    if patches:
        kust["patches"] = patches
    text = dump(kust, header=(
        f"{head}\nBurn Rate application, profile \"{profile}\".\n"
        f"  kubectl kustomize deploy/apps/{profile}        render\n"
        f"  PROFILE={profile} deploy/scripts/deploy.sh    schema job, apply, wait"))
    write("kustomization.yaml", text)
    return stats


# ------------------------------------------------------------------------------------------------------------
# Files shared by both profiles
# ------------------------------------------------------------------------------------------------------------
def gen_schema_job(out: Path, registry: str, pull_secret: str) -> None:
    pod = api_pod("schema", f"{registry}/api:latest", pull_secret, http=False, grace=30, spread_hard=None, restart="Never")
    pod["containers"][0]["resources"] = res("job")
    job = {"apiVersion": "batch/v1", "kind": "Job",
           "metadata": {"generateName": "schema-", "namespace": NAMESPACE,
                        "labels": {**labels("schema", "job"), "app.kubernetes.io/part-of": "burnrate"}},
           "spec": {"backoffLimit": 2, "activeDeadlineSeconds": 600, "ttlSecondsAfterFinished": 86400,
                    "template": {"metadata": {"labels": {**labels("schema", "job"), "app.kubernetes.io/part-of": "burnrate"},
                                              "annotations": NO_MESH},
                                 "spec": pod}}}
    path = out / "jobs" / "schema.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(job, header=(
        "GENERATED by deploy/scripts/gen-apps.sh. Do not edit: change gen_apps.py and re-run.\n"
        "SERVICE=schema: creates the tables of every service (create_all) and exits.\n"
        "deploy.sh puts the image tag of the release in, creates this Job and waits for it BEFORE it applies\n"
        "the Deployments. It is not part of a kustomization: a Job cannot be re-applied.\n"
        "By hand:  kubectl create -f deploy/apps/jobs/schema.yaml  (runs the :latest image)")))


def gen_databases(code: Code, path: Path) -> list[str]:
    names = [s.replace("-", "_") for s in code.services]
    names += [d for d in sorted(code.databases) if d not in names]
    docs = [{"apiVersion": "postgresql.cnpg.io/v1", "kind": "Database",
             "metadata": {"name": d.replace("_", "-"), "namespace": DATA_NS},
             "spec": {"name": d, "owner": PG_ROLE, "cluster": {"name": PG_CLUSTER}}} for d in names]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(*docs, header=(
        "GENERATED by deploy/scripts/gen-apps.sh from SERVICES in burnrate/services/__init__.py (plus any other\n"
        "name passed to make_base() in the code). Do not edit.\n"
        "One PostgreSQL database per service, all owned by role \"burnrate\": BR_DATABASE_URL ends in /{service}\n"
        "and the application puts the service name in, dashes turned into underscores (burnrate/common/db.py).\n"
        "A database nobody uses is harmless; a missing one stops the service at start-up.")))
    return names


def gen_compose_initdb(dbs: list[str], path: Path) -> None:
    """The same databases for the PostgreSQL container of docker-compose.yml."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "# GENERATED by deploy/scripts/gen-apps.sh from SERVICES in burnrate/services/__init__.py. Do not edit.\n"
        "# Runs once, when the PostgreSQL container of docker-compose.yml initialises an empty data directory:\n"
        "# one database per service, like deploy/platform/cnpg/databases.yaml in the cluster.\n"
        "# After adding a service: deploy/scripts/gen-apps.sh, then `docker compose down -v` to start from empty.\n"
        f"for db in {' '.join(dbs)}; do\n"
        '  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \\\n'
        '    -c "CREATE DATABASE \\"$db\\" OWNER \\"$POSTGRES_USER\\""\n'
        "done\n")
    path.chmod(0o755)


def gen_topics(topics: list[str], path: Path) -> None:
    docs = [{"apiVersion": "kafka.strimzi.io/v1", "kind": "KafkaTopic",
             "metadata": {"name": t, "namespace": DATA_NS, "labels": {"strimzi.io/cluster": KAFKA_CLUSTER}},
             "spec": {"topicName": t, "partitions": TOPIC_PARTITIONS_FULL, "replicas": TOPIC_REPLICAS_FULL,
                      "config": {"retention.ms": TOPIC_RETENTION_MS, "cleanup.policy": "delete",
                                 "min.insync.replicas": 2}}} for t in topics]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(*docs, header=(
        "GENERATED by deploy/scripts/gen-apps.sh from deploy/apps/topics.txt plus every topic the code publishes\n"
        "or subscribes to. Do not edit.\n"
        f"Applied in the `full` profile only: {TOPIC_PARTITIONS_FULL} partitions, {TOPIC_REPLICAS_FULL} replicas, "
        "min.insync.replicas 2, 14 days.\n"
        "The `small` profile lets the single broker create topics on first use (3 partitions, 1 replica).")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", default="registry.digitalocean.com/burnrate", help="image prefix (default: %(default)s)")
    ap.add_argument("--pull-secret", default="burnrate", help="imagePullSecrets name = the registry name (default: %(default)s)")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 if the files on disk are out of date")
    ap.add_argument("--quiet", action="store_true", help="print only warnings")
    args = ap.parse_args()

    code = Code()
    read_registry(code)
    scan_code(code)

    # worker -> topics: the code first, deploy/apps/worker-topics.txt on top
    worker_topics = {w: sorted(code.subs.get(w, set())) for w in code.workers}
    source = {w: "code" for w in code.workers if worker_topics[w]}
    for line in read_table(APPS / "worker-topics.txt"):
        if ":" not in line:
            code.warnings.append(f"deploy/apps/worker-topics.txt: line without ':' ignored: {line}")
            continue
        w, _, rest = line.partition(":")
        w = w.strip()
        if w not in code.workers:
            code.warnings.append(f"deploy/apps/worker-topics.txt: {w!r} is not in WORKERS, line ignored")
            continue
        worker_topics[w] = sorted({t.strip() for t in rest.split(",") if t.strip()})
        source[w] = "worker-topics.txt"
    for w in sorted(set(code.subs) - set(code.workers)):
        code.warnings.append(f"the code subscribes with worker={w!r}, which is not in WORKERS: nothing will run those handlers "
                             f"({', '.join(sorted(code.subs[w]))})")
    no_topics = [w for w in code.workers if not worker_topics[w]]

    declared = read_table(APPS / "topics.txt")
    used = set(code.emitted) | {t for ts in worker_topics.values() for t in ts}
    undeclared = sorted(used - set(declared))
    topics = declared + undeclared
    unconsumed = sorted(set(code.emitted) - {t for ts in code.subs.values() for t in ts})
    unpublished = sorted({t for ts in worker_topics.values() for t in ts} - set(code.emitted))

    tmp = Path(tempfile.mkdtemp(prefix="gen-apps-"))
    try:
        stats = {}
        for profile in PROFILES:
            stats[profile] = gen_profile(profile, code, worker_topics, tmp / "apps" / profile, args.registry, args.pull_secret)
        gen_schema_job(tmp / "apps", args.registry, args.pull_secret)
        dbs = gen_databases(code, tmp / "platform" / "cnpg" / "databases.yaml")
        gen_topics(topics, tmp / "platform" / "strimzi" / "topics.yaml")
        gen_compose_initdb(dbs, tmp / "compose" / "initdb.sh")

        rep = ["GENERATED by deploy/scripts/gen-apps.sh: what the generator found in the code. Do not edit.", ""]
        rep.append(f"online services ({len(code.services)}): {', '.join(code.services)}")
        rep.append(f"workers ({len(code.workers)}): {', '.join(code.workers)}")
        rep.append(f"loops ({len(code.loops)}): {', '.join(code.loops)}")
        rep.append(f"jobs ({len(code.jobs)}): {', '.join(code.jobs)}")
        rep.append(f"databases ({len(dbs)}): {', '.join(dbs)}")
        rep.append("")
        for profile, st in stats.items():
            rep.append(f"profile {profile}: {st['deployments']} Deployments ({len(code.services)} services + {len(code.workers)} workers + "
                       f"{len(code.loops)} loops + web), {st['pods']} pods at minimum scale, requests {st['cpu_m']}m CPU / "
                       f"{st['mem_mi']} Mi memory" + (f", {st['meshed_pods']} pods in the mesh" if profile == "full" else ""))
        rep += ["", "worker -> topics (consumer group = worker name)"]
        for w in code.workers:
            rep.append(f"  {w:<22} {', '.join(worker_topics[w]) or '-':<70} [{source.get(w, 'NOT FOUND')}]")
        rep += ["", f"workers without topics ({len(no_topics)}): {', '.join(no_topics) or 'none'}"]
        if no_topics:
            rep.append("  Such a worker exits at start with \"worker <name> has no subscriptions\" (burnrate/common/bus.py) and its")
            rep.append("  pod goes into CrashLoopBackOff. Its ScaledObject has one trigger without a topic.")
        rep += ["", f"topics declared in deploy/apps/topics.txt ({len(declared)}): {', '.join(declared)}",
                (f"topics used by the code but not declared ({len(undeclared)}; added to platform/strimzi/topics.yaml): "
                 f"{', '.join(undeclared) or 'none'}"),
                f"topics published but consumed by no worker ({len(unconsumed)}): {', '.join(unconsumed) or 'none'}",
                f"topics consumed but published nowhere in the code ({len(unpublished)}): {', '.join(unpublished) or 'none'}",
                "", f"warnings ({len(code.warnings)})"]
        rep += [f"  {w}" for w in code.warnings] or ["  none"]
        (tmp / "apps" / "GENERATED.txt").write_text("\n".join(rep) + "\n")

        targets = [(tmp / "apps" / "small", APPS / "small"), (tmp / "apps" / "full", APPS / "full"),
                   (tmp / "apps" / "jobs", APPS / "jobs")]
        singles = [(tmp / "apps" / "GENERATED.txt", APPS / "GENERATED.txt"),
                   (tmp / "platform" / "cnpg" / "databases.yaml", PLATFORM / "cnpg" / "databases.yaml"),
                   (tmp / "platform" / "strimzi" / "topics.yaml", PLATFORM / "strimzi" / "topics.yaml"),
                   (tmp / "compose" / "initdb.sh", ROOT / "deploy" / "compose" / "initdb.sh")]

        def tree(d: Path) -> dict[str, str]:
            return {f.relative_to(d).as_posix(): f.read_text() for f in sorted(d.rglob("*")) if f.is_file()} if d.exists() else {}

        if args.check:
            stale = [dst.relative_to(ROOT).as_posix() for src, dst in targets if tree(src) != tree(dst)]
            stale += [dst.relative_to(ROOT).as_posix() for src, dst in singles if not dst.exists() or src.read_text() != dst.read_text()]
            if stale:
                print("gen-apps: out of date, run deploy/scripts/gen-apps.sh: " + ", ".join(stale), file=sys.stderr)
                return 1
            if not args.quiet:
                print("gen-apps: generated manifests are up to date")
            return 0
        for src, dst in targets:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        for src, dst in singles:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            shutil.copymode(src, dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not args.quiet:
        print("\n".join(rep[2:]))
    elif code.warnings or no_topics:
        for w in code.warnings:
            print(f"gen-apps: warning: {w}", file=sys.stderr)
        if no_topics:
            print(f"gen-apps: warning: workers without topics: {', '.join(no_topics)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
