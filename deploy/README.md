# Burn Rate: deployment

Everything needed to run Burn Rate on DigitalOcean Kubernetes: two container images, the platform
(gateway, Kafka, PostgreSQL, Valkey, autoscaling, monitoring), the application manifests and the scripts
that install them. Written on 15 November 2026. **Nothing here has run on a cluster yet**; what was and was
not checked is listed under [Verified and not verified](#verified-and-not-verified).

```
Dockerfile, web/Dockerfile, web/nginx.conf   the two images (API: one image, role chosen by SERVICE)
docker-compose.yml                           PostgreSQL + Kafka + Valkey + the API image on a laptop
deploy/
  versions.env            every pinned chart version, node size and count: the one place to change them
  scripts/
    cluster.sh            create the DOKS cluster and the registry (prints the cost; needs --yes)
    bootstrap.sh          install the platform for PROFILE (idempotent; --dry-run renders without installing)
    secrets.sh            generate and apply the application Secret
    build.sh              build both images for linux/amd64 and push them
    deploy.sh             schema job, apply the application, wait for the rollouts, print the URL
    smoke.sh              check the public URL from outside
    teardown.sh           delete the cluster, its load balancer and volumes (dry run unless --yes)
    gen-apps.sh           regenerate the per-service manifests from burnrate/services/__init__.py
    validate.sh           everything that can be checked without a cluster (charts, CRD schemas, budget)
    lib.sh, gen_apps.py, check_schemas.py, budget.py     helpers
  platform/<component>/   Helm values and manifests of the platform
  apps/
    common/               written by hand: ConfigMap, HTTPRoute, NetworkPolicy, PodMonitor, alert rules
    small/ full/ jobs/    GENERATED: Deployments, Services, autoscaling, the schema Job
    env/                  hostname, image tags, config hash of one deployment (rewritten by deploy.sh)
    topics.txt            the Kafka topics; worker-topics.txt: manual overrides for the KEDA triggers
    GENERATED.txt         what the generator found in the code (services, worker -> topics, warnings)
    secret.example.yaml   shape of the Secret (never applied)
  compose/initdb.sh       GENERATED: the per-service databases for docker-compose
  secrets/  .state/       created at run time, gitignored: generated passwords / last load balancer IP, tag, history
```

## The two profiles

`PROFILE=small` is the default everywhere. `PROFILE=full` is for after the account limit is raised.

| | small (default) | full |
|---|---|---|
| nodes | **2 x s-4vcpu-8gb** in nyc1 (about 12.5 GiB allocatable) | 6 or more of the same size |
| public entry | Caddy on the droplet `burnrate-app` holds the certificate and forwards plain HTTP to the cluster's load balancer. Gateway: one HTTP listener | TLS at the gateway, cert-manager + Let's Encrypt, HTTP redirects to HTTPS at the gateway |
| public name | `burnrate.67.207.94.96.sslip.io` (fixed) | `burnrate.<load-balancer-IP-with-dashes>.sslip.io` |
| PostgreSQL | 2 instances | 3 instances, one synchronous |
| Kafka | 1 node, replication 1, topics created on first use | 3 brokers, replication 3, `min.insync.replicas` 2, topics declared |
| application | edge-api 2, web 2, engine 2, everything else 1 | 2 or more of everything (timer-dispatcher stays 1) |
| service mesh | **none** (does not fit, see the budget) | Linkerd, 3 control-plane replicas |
| also | | Argo CD, Vault (Raft, 3 pods), Loki, Tempo, Alloy |

## What gets installed

Versions are pinned in `deploy/versions.env`. "seruen" = ran on the previous cluster (DOKS 1.35.9, October
2026); "new" = looked up on 2026-11-15 and not yet run anywhere.

| component | namespace | version | source | profile |
|---|---|---|---|---|
| Kubernetes | | newest 1.35.x offered (1.35.10-do.0) | seruen ran 1.35.9 | both |
| Envoy Gateway + Gateway `edge/burnrate` | envoy-gateway-system, edge | chart 1.9.3 (Gateway API v1.6.1, supports Kubernetes 1.33-1.36) | new | both |
| metrics-server | kube-system | chart 3.14.0 (0.9.0) | seruen | both |
| Strimzi + Kafka `burnrate` | platform, data | 1.3.0, Kafka 4.3.1 | seruen | both |
| CloudNativePG + cluster `pg` | platform, data | chart 0.29.1 (operator 1.30.1), image `postgresql:17.12-standard-trixie` | seruen (image: new) | both |
| Valkey | data | `valkey/valkey:8.1.11-alpine` | new (seruen ran Redis 7.4) | both |
| KEDA | platform | 2.21.0 | seruen | both |
| kube-prometheus-stack (Prometheus, Alertmanager, Grafana, node-exporter, kube-state-metrics) | observability | chart 92.10.1 | seruen | both (small: 1 day, 5 GiB) |
| NetworkPolicies | data, burnrate | | | both |
| cert-manager + ClusterIssuers + Certificate | cert-manager, edge | v1.21.2 | seruen | full |
| Linkerd edge | linkerd | 2026.10.1 (edge-26.10.1) | seruen | full |
| Loki / Tempo / Alloy | observability | 7.3.0 / 1.24.4 / 1.14.0 | seruen | full |
| Argo CD | argocd | chart 10.10.5 (3.5.5) | seruen | full |
| Vault, Raft, 3 pods | platform | chart 0.34.1 (2.0.4) | seruen ran dev mode; Raft is new | full |

ingress-nginx is not used: upstream retired it in March 2026.

## First deployment

Needs on the laptop: `doctl`, `kubectl`, `helm`, `docker` (buildx), `jq`, `openssl`, `python3`, `uv`.
The DigitalOcean token is read from `$DIGITALOCEAN_ACCESS_TOKEN`, else from `~/.do_token`.

```bash
cd burnrate

deploy/scripts/validate.sh --all            # offline checks; touches nothing
deploy/scripts/cluster.sh                   # shows what will be created and the monthly cost; creates nothing
deploy/scripts/cluster.sh --yes             # 2 x s-4vcpu-8gb in nyc1 + registry "burnrate"   (5-8 min)

deploy/scripts/bootstrap.sh --dry-run       # renders every chart, prints every step; installs nothing
deploy/scripts/bootstrap.sh                 # the platform (10-15 min); prints the load balancer IP at the end

# on the droplet burnrate-app: point Caddy at that IP, see "The edge server" below

export BR_OPENAI_API_KEY=sk-...             # or put it in burnrate/.env
deploy/scripts/secrets.sh --dry-run         # which keys, from where; no values are ever printed
deploy/scripts/secrets.sh                   # Secret burnrate/burnrate-secrets

deploy/scripts/build.sh                     # both images, linux/amd64, pushed with tag <git short sha> and latest
deploy/scripts/deploy.sh --dry-run          # renders what would be applied; no cluster needed
deploy/scripts/deploy.sh                    # schema job, apply, wait for 30 Deployments
deploy/scripts/smoke.sh                     # 7 checks against https://burnrate.67.207.94.96.sslip.io
```

`bootstrap.sh` steps for small, in order: `namespaces registry gateway metrics_server kafka postgres valkey
keda observability netpol`. Each can be run alone (`bootstrap.sh kafka`) and re-run.
After the first `bootstrap.sh`, look at what DigitalOcean's own pods take (the one number in the budget that
could not be measured): `kubectl describe nodes | grep -A8 'Allocated resources'`.

### If the registry cannot be created

The basic plan allows **one** registry per account, and the previous project's registry `seruen` was kept.
`cluster.sh --yes` then stops with the two options: `export REGISTRY_NAME=seruen` for every script (images
become `registry.digitalocean.com/seruen/api` and `.../web`), or delete the old registry yourself.
Registry names are also unique across all of DigitalOcean, so `burnrate` may simply be taken.

### How build.sh gets linux/amd64 images out of an arm64 Mac

`docker buildx build --platform linux/amd64` for both images. For the API image the whole build runs as
amd64 under Docker Desktop's emulation; it only unpacks ready-made wheels, so it takes seconds. For the web
image the Node build stage is pinned to the Mac's own architecture (`FROM --platform=$BUILDPLATFORM`), because
its output is static files, and only the final nginx stage is amd64. Checked on this Mac: both images
report `Architecture: amd64` and start.

## The edge server

The jury has `https://burnrate.67.207.94.96.sslip.io`. That name points at the droplet `burnrate-app`
(67.207.94.96, nyc1), where Caddy holds the Let's Encrypt certificate. The cluster sits behind it:

```
browser --HTTPS--> Caddy on burnrate-app --HTTP, same Host header--> load balancer :80 --> Envoy --> edge-api / webhooks / web
```

In the Caddyfile on the droplet (usually `/etc/caddy/Caddyfile`), replace the body of the site block.
`LB_IP` is the address `bootstrap.sh` and `deploy.sh` print (also in `deploy/.state/lb-ip`):

```
burnrate.67.207.94.96.sslip.io {
	reverse_proxy http://LB_IP:80 {
		# Caddy keeps the Host header as the browser sent it; the cluster routes on exactly this name.
		header_up Host {host}
		# server-sent events of the rehearsal chat: pass every chunk on at once
		flush_interval -1
	}
}
```

Then `sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.
Caddy redirects `http://` to `https://` by itself and sets `X-Forwarded-For` to the visitor's address, which
is what the API's rate limiter reads. Do **not** add a redirect on the cluster side.
To keep the old site available while the cluster is being checked, test the cluster first without touching
Caddy: `curl -H 'Host: burnrate.67.207.94.96.sslip.io' http://LB_IP/api/health` (or `smoke.sh --lb LB_IP`).

The load balancer address changes if the gateway's Service or the cluster is re-created: update the Caddyfile then.

## Updating and rolling back

```bash
deploy/scripts/build.sh && deploy/scripts/deploy.sh && deploy/scripts/smoke.sh
```

- A new service or worker: one line in `burnrate/services/__init__.py`; `deploy.sh` regenerates the
  manifests (or `deploy/scripts/gen-apps.sh` to see the result first). `deploy/apps/GENERATED.txt` lists
  every worker with its topics, and any worker without a topic: such a worker exits at start.
- Only configuration changed (`deploy/apps/common/configmap.yaml`) or the Secret: `deploy.sh` alone; it
  stamps a hash of both on the pod templates, so the Deployments roll.
- **Roll back**: `deploy/scripts/deploy.sh --history`, then `deploy/scripts/deploy.sh --tag <previous tag>`.
  The schema job only creates missing tables, it never drops anything, so an older image runs on a newer
  schema as long as the newer code did not need a column the old code lacks. One Deployment only:
  `kubectl -n burnrate rollout undo deployment/<name>`.
- A failed schema job stops the release before any Deployment is touched.
- Platform: change the version in `deploy/versions.env` or a values file, `validate.sh --all`, then
  `bootstrap.sh <step>`.

## Memory budget, profile small

Two nodes of 8 GB give about 6.26 GiB allocatable each (measured on the previous cluster for this size):
**12 820 Mi**. Requests, computed from the rendered charts and manifests by `validate.sh --budget`:

| namespace | pods | memory requests | what |
|---|---|---|---|
| burnrate | 33 | 4 416 Mi | 26 x 128 Mi (16 services, 9 workers, timer-dispatcher), edge-api 2 x 128, engine 2 x 256, statement-extractor 256, web 2 x 32 |
| data | 5 | 1 920 Mi | Kafka 1 024, PostgreSQL 2 x 384, Kafka exporter 64, Valkey 64 |
| observability | 7 | 1 136 Mi | Prometheus 528, Grafana 384, kube-state-metrics 64, operator 64, Alertmanager 48, node-exporter 2 x 24 |
| platform | 5 | 512 Mi | Strimzi 256, CloudNativePG 96, KEDA 160 |
| envoy-gateway-system | 3 | 448 Mi | 2 Envoy proxies x 160, controller 128 |
| kube-system (ours) | 1 | 64 Mi | metrics-server |
| **what this repository installs** | **54** | **8 496 Mi** | |
| kube-system (DigitalOcean: Cilium, CoreDNS, CSI, agents) | | up to 1 988 Mi | measured on the previous 4-node cluster; fewer nodes run fewer of these pods, so this is the upper bound. **Not measured on 2 nodes.** |
| **total** | | **up to 10 484 Mi = 82 %** | |
| **headroom** | | **at least 2 336 Mi (18 %)** | about 3 GiB if DigitalOcean's share shrinks with the node count |

CPU requests: 3 760m of ours on 8 vCPU (DigitalOcean's share not known). To get there an idle service
reserves 50m instead of 100m in this profile; edge-api, funnel (100m), engine and statement-extractor (200m)
keep the planned figures. Limits are unchanged (500m / 384 Mi; 1 CPU / 512 Mi for the two heavy ones).

Measured on this laptop (amd64 image under emulation, idle): one service in cluster mode 75 MiB, one worker
69 MiB, every service in one process 84-125 MiB. So 128 Mi is an honest request, not an optimistic one.

What the headroom is for, and what it is not enough for:
- A rollout starts one extra pod per online Deployment before it stops the old one (about 2.4 GiB if all 19
  did it at once); with less free memory the extra pods wait their turn. Workers and the timer loop stop
  the old pod first and need no extra memory.
- Autoscaling ceilings are low on purpose: edge-api and engine 2 to 3, funnel 1 to 2, web 2 to 3, every
  worker 1 to 2. Everything at its ceiling at once would ask for about 1.9 GiB more; pods that do not fit
  stay Pending and nothing else is harmed.
- **Losing one of the two nodes**: the half of the pods that lived there cannot all move. edge-api, engine,
  web, the Envoy proxies and PostgreSQL have a copy on each node and keep serving; services with one replica
  that lived on the lost node are down until it returns.

Compared with the three-node plan this profile gave up: the service mesh, cert-manager, 512 Mi of Kafka
(heap 512 MB instead of 768 MB), 256 Mi of PostgreSQL and 256 Mi of Prometheus.

**Linkerd is left out of small.** It would cost 840 Mi of requests: 220 Mi for its control plane (measured
on the previous cluster, and the same in the rendered chart) plus 20 Mi for the proxy in each of 31 pods.
That is more than a third of the headroom above. Without it, traffic between services inside the cluster is
plain HTTP, protected by the NetworkPolicies and the internal token but not encrypted or mutually
authenticated. It is in `full`.

Profile full asks for 24 160 Mi (113 pods, with Linkerd) plus DigitalOcean's share: about 27 GiB, six nodes
of this size at 70 %.

## Logs and metrics

```bash
kubectl -n burnrate get pods                                           # everything of the application
kubectl -n burnrate logs deploy/edge-api --since=15m                   # one service (add -f to follow)
kubectl -n burnrate logs -l app.kubernetes.io/component=worker --tail=50 --prefix   # all workers
kubectl -n burnrate logs job/<schema-job>                              # the last schema job
kubectl -n envoy-gateway-system logs -l gateway.envoyproxy.io/owning-gateway-name=burnrate --tail=50   # access log
kubectl top pods -n burnrate --sort-by=memory

kubectl -n observability port-forward svc/kps-grafana 3000:80          # http://localhost:3000
#   user admin, password: deploy/secrets/grafana.env ; folder "Burn Rate" -> "Burn Rate overview"
kubectl -n observability port-forward svc/kps-prometheus 9090          # alerts: http://localhost:9090/alerts
kubectl -n burnrate port-forward svc/backoffice 8001:8000              # backoffice API, header x-admin-token
```

Grafana and Prometheus are deliberately not public. In small there is no log store: `kubectl logs` reads
what the node still has (full adds Loki).

- Dashboard `deploy/platform/grafana-dashboards/burnrate-overview.json`: request rate, 5xx share and p95 per
  service, Kafka consumer lag per worker (and per topic), slowest routes, restarts, memory per pod.
- Alerts `deploy/apps/common/monitoring.yaml`: 5xx above 2 % for 5 minutes; p95 above 1 s for 10 minutes
  (llm-gateway and the rehearsal routes are left out: they wait for a language model); consumer lag growing
  for 10 minutes; a container restarted / keeps restarting; certificate expiring in under 14 days.
  The certificate alert has data only in full: in small the certificate lives on the Caddy droplet and
  `smoke.sh` checks its remaining days from outside.
- No receiver is configured in Alertmanager: alerts are visible in Prometheus and Grafana, nobody is paged.

## How the workloads are set up

- Every pod: `SERVICE`, `OTEL_SERVICE_NAME`, the ConfigMap `burnrate-config` and the Secret `burnrate-secrets`
  as environment; non-root (uid 10001), read-only root filesystem with `/tmp` and `/data` as emptyDir, no
  capabilities, seccomp RuntimeDefault.
- Online services: startup and liveness probes on `/healthz`, readiness on `/readyz`; 5 s `preStop` sleep,
  then uvicorn drains (grace 30 s; 75 s for edge-api, rehearsal, llm-gateway, which carry streams).
- **Workers and timer-dispatcher have no probes.** They have no HTTP server, and an exec probe could only
  ask "does the process exist", which Kubernetes already knows. Instead: a crash restarts the container
  (alert on restarts), a stuck consumer shows as growing lag (alert), and `minReadySeconds: 20` keeps a
  rollout from calling a pod that dies at once a success. The image runs `tini` as PID 1 so that these
  processes stop on SIGTERM at once (measured: 1 s) instead of hanging for the whole grace period.
- Spread over nodes: soft in small (a hard rule can block a rollout on two full nodes), hard in full.
  PodDisruptionBudget (`maxUnavailable: 1`) for everything with two or more replicas.
- Autoscaling: HorizontalPodAutoscaler at 70 % CPU of the app container for edge-api, engine, funnel, web;
  a KEDA ScaledObject on Kafka consumer lag for every worker (consumer group = worker name, one trigger per
  topic, read from `@subscribe(...)` in the code).
- `schema` runs as a Job before each rollout; `retention` is a CronJob at 03:17 New York time.

## Routing, and how to test that internal paths are closed

`deploy/apps/common/routes.yaml`: `/api` -> edge-api:8000, `/hooks` -> webhooks:8000, the rest -> web:8080.
Request timeouts: 600 s for `/api/rehearsal` (the chat stream), 180 s for `/api/statements` (uploads up to
10 MB; Envoy has no body-size cap of its own, the application enforces the limit), 60 s for the rest of the API.

`/internal`, `/metrics`, `/readyz` and `/admin/api` are answered `404` by the gateway and never forwarded.
Behind that rule: the NetworkPolicy lets the gateway reach only edge-api, webhooks and web; and internal
routes need `x-internal-token`, a header the gateway strips from every incoming request.

```bash
deploy/scripts/smoke.sh        # checks 3-5, or by hand:
curl -s -o /dev/null -w '%{http_code}\n' https://burnrate.67.207.94.96.sslip.io/internal/engine/rules                      # 404
curl -s -o /dev/null -w '%{http_code}\n' https://burnrate.67.207.94.96.sslip.io/metrics                                    # 404
curl -s -o /dev/null -w '%{http_code}\n' --path-as-is https://burnrate.67.207.94.96.sslip.io/api/../internal/engine/rules  # 404
```

Network policy of the data namespace (`deploy/platform/network-policies/data.yaml`): default deny; the
client ports of PostgreSQL, Kafka and Valkey are open to the application namespace only. Three narrow
additions without which the stores do not run: pods of the data namespace among themselves (replication),
the operators in `platform` (CloudNativePG to its instances; KEDA to Kafka to read lag), and Prometheus to
the exporters. The file has a two-line test.

## Secrets

Nothing secret is in git. `deploy/secrets/` (gitignored, mode 700) holds what was generated:

| file | what | Kubernetes Secret |
|---|---|---|
| `app.env` | `BR_JWT_SECRET`, `BR_INTERNAL_TOKEN`, `BR_ADMIN_TOKEN` | `burnrate/burnrate-secrets` |
| `pg.env` | password of the PostgreSQL role `burnrate` | `data/pg-burnrate`, and inside `BR_DATABASE_URL` in `burnrate/burnrate-secrets` |
| `grafana.env` | Grafana admin | `observability/grafana-admin` |

`BR_OPENAI_API_KEY` (and optionally the Anthropic, Stripe, Twilio, SMTP values) come from your environment or
`burnrate/.env` each time `secrets.sh` runs; they are stored only in the cluster.

Rotate:

```bash
deploy/scripts/secrets.sh --rotate jwt --restart        # signs everybody out
deploy/scripts/secrets.sh --rotate internal --restart   # calls between old and new pods fail until all restarted
deploy/scripts/secrets.sh --rotate admin --restart
deploy/scripts/secrets.sh --rotate db --restart         # CloudNativePG changes the role's password, pods reconnect
BR_OPENAI_API_KEY=sk-new deploy/scripts/secrets.sh --restart
```

Spending guard for the language model is configuration, not a secret: `BR_LLM_DAILY_BUDGET_USD=3.0` for
the whole deployment, 0.15 per user, 0.03 per demo guest, in `deploy/apps/common/configmap.yaml`.

## Known limits

- **One data centre** (nyc1). DigitalOcean has no availability zones inside a region.
- **Two nodes** with no room to lose one (see the budget). No surge upgrades: there is no fourth droplet.
- **The hop from Caddy to the load balancer is plain HTTP over public addresses**: session cookies and
  uploaded statements travel unencrypted between two DigitalOcean machines in the same region. And the load
  balancer's port 80 answers anyone who learns its address and sends the right Host header (the application
  is the same one the public sees, but without TLS, and such a caller can invent `X-Forwarded-For`).
  Closing it to everything but the droplet is one annotation, written but **not enabled because untried**:
  `deploy/platform/envoy-gateway/proxy-small.yaml`.
- **Caddy is a single point of entry** on one droplet.
- **One Kafka broker in small**, replication 1. Events are first rows in each service's outbox table, so a
  lost broker loses no committed business data, but events in flight wait or are lost with its volume.
- **No service mesh in small**: no mutual TLS between services.
- **No PostgreSQL backups** in either profile (needs an object-storage bucket and its keys).
- No PgBouncer: the asyncpg driver uses prepared statements. `max_connections` is 300 (small).
- Valkey is one pod; the rate limiter fails open while it is away.
- Vault (full) has to be unsealed by hand after every pod restart, and the application does not read it yet.
- Argo CD (full) is installed with a manual-sync Application; `deploy.sh` stays the way to release.
- The registry's basic plan holds 5 GiB: after a few dozen builds run
  `doctl registry garbage-collection start --include-untagged-manifests`.
- A service removed from the code is not deleted from the cluster by `deploy.sh`.

## Cost per month

| | | checked today on digitalocean.com |
|---|---|---|
| 2 nodes s-4vcpu-8gb | 2 x $48 = **$96.00** | yes ($48.00 a month, $0.07143 an hour, billed by the second) |
| load balancer, 1 node | **$12.00** | yes ($12 per node for a regional load balancer) |
| registry, basic plan | **$5.00** | yes (1 registry, 5 repositories, 5 GiB) |
| volumes, 36 GiB (PostgreSQL 2 x 10, Kafka 10, Prometheus 5, Valkey 1) | 36 x $0.10 = **$3.60** | yes ($0.10 per GiB) |
| DOKS control plane, not highly available | $0 | yes |
| **cluster total** | **$116.60**, about $3.90 a day | |
| edge droplet `burnrate-app`, s-2vcpu-4gb (exists already) | about $24 | no: from memory, not looked up |

Not checked: taxes, outbound traffic beyond the included allowance, and what the account is actually
billed (compare with the invoice of the previous cluster).
Profile full on six nodes: 6 x $48 + $12 + $5 + 176 GiB x $0.10 = $322.60.

## Teardown

```bash
deploy/scripts/teardown.sh          # dry run: lists the cluster, its volumes and its load balancer
deploy/scripts/teardown.sh --yes    # deletes them; ALL data in the cluster is lost
```

The registry and the edge droplet are never touched. Before creating a new cluster, remove `deploy/secrets/`
so that new passwords are generated, and expect a new load balancer address for the Caddyfile.

## Verified and not verified

Verified on a laptop, 2026-11-15 (no cluster, no cloud call, no image pushed):

- Both images build for linux/amd64 on an arm64 Mac and start as non-root with a read-only root filesystem
  and no capabilities. API in local mode: `/healthz` 200. Web: app shell 200, `/internal/...`, `/metrics`,
  `/api/...` 404, hashed assets `immutable`, `index.html` `no-cache`; no content-security-policy violation
  when the built app loads in a browser.
- `docker compose up`: the API image against PostgreSQL with the `{service}` database URL; then in cluster
  mode `SERVICE=schema` created the tables in the per-service databases, the worker `notifier` joined
  consumer group `notifier` on Kafka, the service `cards` started with its Kafka relay.
- `validate.sh --all`: `bash -n` on every script; generated manifests match the code; `kubectl kustomize`
  renders both profiles with 30 Deployments each (18 services + 10 workers + timer-dispatcher + web);
  `helm template` of every chart with these values (6 charts in small, 14 in full); every custom resource
  (Gateway, HTTPRoute, EnvoyProxy, Kafka, Cluster, Database, ScaledObject, PodMonitor, PrometheusRule,
  Certificate, ...) checked against the CRD schemas shipped in the pinned charts.
- Also run by hand: shellcheck (clean), actionlint on the workflows (clean), `promtool check rules` on the
  7 alert rules, kubeconform in strict mode against Kubernetes 1.35 for the built-in kinds (166 valid, 0 invalid).
- `bootstrap.sh --dry-run` ran for both profiles **before** the change to two nodes and the edge server
  (every chart rendered, no kubectl or doctl call was made). Since that change `bootstrap.sh` has not been
  executed in any mode, and `deploy.sh` has never been executed: start with their `--dry-run`.

Not verified, because it needs the cluster:

1. **Envoy Gateway 1.9.3 on DOKS.** New here. Whether the Gateway API CRDs that DOKS preinstalls are new
   enough for it (it is built for v1.6.1; Helm will not replace existing CRDs), and whether DOKS also runs
   Cilium's own Gateway controller. A second controller should not interfere (different GatewayClass);
   `bootstrap.sh gateway` prints the CRD version and the GatewayClasses it finds before installing.
2. **The route through Caddy**: that the Host header arrives unchanged, that server-sent events are not
   buffered, that a 10 MB upload passes. `smoke.sh` covers the basics; the chat stream and an upload need a
   click through the product.
3. **The load balancer** created for the Envoy Service (annotations, `externalTrafficPolicy: Cluster`), and
   the untested allow-rule annotation.
4. **The NetworkPolicies** on DOKS's Cilium: that the operators still reach PostgreSQL and Kafka (the
   `netpol` step checks both afterwards and says how to undo) and that the gateway's pod labels are the ones
   the policy expects.
5. **Memory on two nodes**: DigitalOcean's own share, and Kafka in 1 GiB with a 512 MB heap.
6. KEDA's Kafka triggers, the HorizontalPodAutoscaler on the `app` container, the CloudNativePG `Database`
   objects and the managed role password, the Strimzi listener `networkPolicyPeers`: valid against the
   schemas, not run.
7. Everything in `full` that the previous cluster did not have: Vault in Raft mode, three Kafka brokers,
   Linkerd next to Envoy Gateway, cert-manager's HTTP-01 through a Gateway (the previous cluster used
   ingress-nginx), Let's Encrypt limits for `sslip.io` names.
8. `cluster.sh --yes`, `teardown.sh`, the non-dry paths of `secrets.sh` and `deploy.sh`: reviewed and
   linted, never executed.

Two things in the application that the deployment depends on (not changed here):

- The outbox relay of a service starts its Kafka producer once, at start-up, outside the retry loop
  (`burnrate/common/bus.py`, `kafka_relay_loop`). If Kafka is unreachable at that moment the relay task ends
  and that pod publishes no events until it restarts. Kafka must be Ready before the application starts
  (`bootstrap.sh` waits for it); after a Kafka outage, restart the Deployments.
- The relay stops at the first event it cannot publish, so topic creation on first use stays enabled.
  The code publishes two topics that are not in the planned list, `notify.inbound` and `subscription.ended`
  (`deploy/apps/GENERATED.txt`); the generator declares them for full.
