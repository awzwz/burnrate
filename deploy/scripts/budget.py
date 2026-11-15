#!/usr/bin/env python3
"""Add up the CPU and memory REQUESTS of everything a profile installs, from rendered manifests (no cluster).

    budget.py --nodes 3 [--mesh] <namespace>=<file.yaml> ...

Each argument is a rendered file and the namespace its objects go to when they do not name one themselves
(Helm leaves metadata.namespace out of most objects). validate.sh --budget renders the charts and calls this.

Counted per pod: every container plus native sidecars (init containers with restartPolicy Always).
  Deployment / StatefulSet   replicas (or the minimum of the HorizontalPodAutoscaler / KEDA ScaledObject that owns it)
  DaemonSet                  one pod per node (--nodes)
  Prometheus, Alertmanager   spec.replicas x (spec.resources + config reloader)
  KafkaNodePool, Kafka       brokers, Kafka Exporter, Entity Operator
  Cluster (CloudNativePG)    instances x resources
  EnvoyProxy                 replicas x (envoy + shutdown manager)
  --mesh                     adds a Linkerd proxy (10m, 20Mi) to every pod of namespace burnrate that does not opt out
Not counted: Jobs and CronJobs (they come and go), and what DigitalOcean runs in kube-system (not in any chart).

Needs PyYAML:  uv run --no-project --isolated --with pyyaml python budget.py ...
"""
from __future__ import annotations

import sys

import yaml

RELOADER = ("10m", "16Mi")  # prometheusOperator.prometheusConfigReloader.resources in kube-prometheus-stack/values.yaml
ENVOY_SHUTDOWN = ("10m", "32Mi")  # Envoy Gateway's default for the shutdown-manager container (not in any values file)
LINKERD_PROXY = ("10m", "20Mi")  # proxy.resources in linkerd/values.yaml


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor("tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node))


def cpu(v) -> int:
    if v is None:
        return 0
    s = str(v)
    return int(s[:-1]) if s.endswith("m") else int(float(s) * 1000)


def mem(v) -> int:
    """MiB."""
    if v is None:
        return 0
    s = str(v)
    for suffix, factor in (("Ki", 1 / 1024), ("Mi", 1), ("Gi", 1024), ("k", 1 / 1048.576), ("M", 0.95367), ("G", 953.67)):
        if s.endswith(suffix):
            return round(float(s[: -len(suffix)]) * factor)
    return round(float(s) / 1048576)


def req(resources: dict | None) -> tuple[int, int]:
    r = (resources or {}).get("requests") or {}
    return cpu(r.get("cpu")), mem(r.get("memory"))


def pod_req(spec: dict) -> tuple[int, int]:
    c = m = 0
    for container in spec.get("containers") or []:
        a, b = req(container.get("resources"))
        c, m = c + a, m + b
    for container in spec.get("initContainers") or []:
        if container.get("restartPolicy") == "Always":
            a, b = req(container.get("resources"))
            c, m = c + a, m + b
    return c, m


def main() -> int:
    args = sys.argv[1:]
    nodes, mesh, files = 3, False, []
    while args:
        a = args.pop(0)
        if a == "--nodes":
            nodes = int(args.pop(0))
        elif a == "--mesh":
            mesh = True
        elif "=" in a:
            files.append(tuple(a.split("=", 1)))
        else:
            print(__doc__)
            return 2
    if not files:
        print(__doc__)
        return 2

    docs = []
    for ns, path in files:
        with open(path) as f:
            for d in yaml.load_all(f, Loader=Loader):
                if isinstance(d, dict) and d.get("kind"):
                    d.setdefault("metadata", {}).setdefault("namespace", ns)
                    docs.append(d)

    floor: dict[tuple[str, str], int] = {}  # (namespace, deployment) -> minimum replicas set by an autoscaler
    for d in docs:
        ns = d["metadata"]["namespace"]
        if d["kind"] == "HorizontalPodAutoscaler":
            floor[(ns, d["spec"]["scaleTargetRef"]["name"])] = d["spec"].get("minReplicas", 1)
        elif d["kind"] == "ScaledObject":
            floor[(ns, d["spec"]["scaleTargetRef"]["name"])] = d["spec"].get("minReplicaCount", 0)

    rows: list[tuple[str, str, int, int, int]] = []  # namespace, component, pods, cpu per pod, MiB per pod
    for d in docs:
        kind, ns, name, spec = d["kind"], d["metadata"]["namespace"], d["metadata"].get("name", "?"), d.get("spec") or {}
        if kind in ("Deployment", "StatefulSet", "DaemonSet"):
            tmpl = spec.get("template") or {}
            c, m = pod_req(tmpl.get("spec") or {})
            if kind == "DaemonSet":
                n = nodes
            else:
                n = spec["replicas"] if "replicas" in spec else floor.get((ns, name), 1)
            ann = (tmpl.get("metadata") or {}).get("annotations") or {}
            if mesh and ns == "burnrate" and ann.get("linkerd.io/inject") != "disabled":
                c, m = c + cpu(LINKERD_PROXY[0]), m + mem(LINKERD_PROXY[1])
            if n:
                rows.append((ns, f"{name} ({kind})", n, c, m))
        elif kind in ("Prometheus", "Alertmanager"):
            c, m = req(spec.get("resources"))
            rows.append((ns, f"{name} ({kind} + config reloader)", spec.get("replicas", 1),
                         c + cpu(RELOADER[0]), m + mem(RELOADER[1])))
        elif kind == "KafkaNodePool":
            c, m = req(spec.get("resources"))
            rows.append((ns, f"kafka pool {name} (KafkaNodePool)", spec.get("replicas", 1), c, m))
        elif kind == "Kafka":
            if "kafkaExporter" in spec:
                c, m = req(spec["kafkaExporter"].get("resources"))
                rows.append((ns, "kafka exporter (Kafka)", 1, c, m))
            eo = spec.get("entityOperator") or {}
            if eo:
                c = m = 0
                for part in ("topicOperator", "userOperator"):
                    a, b = req((eo.get(part) or {}).get("resources"))
                    c, m = c + a, m + b
                rows.append((ns, "kafka entity operator (Kafka)", 1, c, m))
        elif kind == "Cluster" and d.get("apiVersion", "").startswith("postgresql.cnpg.io/"):
            c, m = req(spec.get("resources"))
            rows.append((ns, f"postgresql {name} (Cluster)", spec.get("instances", 1), c, m))
        elif kind == "EnvoyProxy":
            k = ((spec.get("provider") or {}).get("kubernetes") or {})
            dep = k.get("envoyDeployment") or {}
            c, m = req((dep.get("container") or {}).get("resources"))
            rows.append((ns, f"envoy proxies of the gateway (EnvoyProxy {name})", dep.get("replicas", 1),
                         c + cpu(ENVOY_SHUTDOWN[0]), m + mem(ENVOY_SHUTDOWN[1])))

    rows.sort(key=lambda r: (r[0], -r[2] * r[4], r[1]))
    total_c = total_m = total_p = 0
    by_ns: dict[str, list[int]] = {}
    print(f"{'namespace':<22}{'component':<62}{'pods':>5}{'CPU m':>8}{'MiB':>7}{'total m':>9}{'total MiB':>11}")
    for ns, comp, n, c, m in rows:
        print(f"{ns:<22}{comp:<62}{n:>5}{c:>8}{m:>7}{n * c:>9}{n * m:>11}")
        agg = by_ns.setdefault(ns, [0, 0, 0])
        agg[0] += n
        agg[1] += n * c
        agg[2] += n * m
        total_p, total_c, total_m = total_p + n, total_c + n * c, total_m + n * m
    print()
    print(f"{'namespace':<22}{'pods':>6}{'CPU m':>9}{'MiB':>9}")
    for ns in sorted(by_ns):
        print(f"{ns:<22}{by_ns[ns][0]:>6}{by_ns[ns][1]:>9}{by_ns[ns][2]:>9}")
    print(f"{'TOTAL (rendered)':<22}{total_p:>6}{total_c:>9}{total_m:>9}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
