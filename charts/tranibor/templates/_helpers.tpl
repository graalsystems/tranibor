{{/*
Short name and labels common to all the platform's objects. The name is not the chart's: the chart
is published as `tranibor` and as `choregos` (ADR 0176 R3), and a deployment that moves from one
to the other must render the same objects (`tests/charts/test_chart_names.py`).
*/}}
{{- define "tranibor.name" -}}
{{- default (include "tranibor.labelPrefix" .) .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
The values of `app.kubernetes.io/name` and `app.kubernetes.io/part-of` on the platform's own pods (ADR 0226,
R9b). A label has one value, and an operator's NetworkPolicies, Cilium policies, log and metric queries may
select the former one (`choregos-api`, `part-of: choregos`): renamed in place, they would stop matching
without a word. So the former values stay the default for the window, and `global.labelValues: tranibor`
moves a deployment to `tranibor-api`, `part-of: tranibor`… once its own selectors admit either. The
Deployments that carry these pods select on `app.kubernetes.io/component` (and the queue), never on the
name, so the switch recreates nothing; the chart's own NetworkPolicies and the code admit either value.
0.32.0 makes `tranibor` the only value. Objects named after the release (`<release>-postgresql`,
`-litellm`, `-temporal`…) keep their former values: their selectors cannot change.
*/}}
{{- define "tranibor.labelPrefix" -}}
{{- $chosen := (.Values.global | default dict).labelValues | default "former" -}}
{{- if not (has $chosen (list "former" "tranibor")) -}}
{{- fail (printf "global.labelValues=%s: `former` (the default until 0.32.0) or `tranibor` (ADR 0226)" $chosen) -}}
{{- end -}}
{{- ternary "tranibor" "choregos" (eq $chosen "tranibor") -}}
{{- end -}}

{{/* `<prefix>-<name>`: the `app.kubernetes.io/name` of one of the platform's pods. `(dict "root" $ "name" "api")`. */}}
{{- define "tranibor.podName" -}}
{{- printf "%s-%s" (include "tranibor.labelPrefix" .root) .name -}}
{{- end -}}

{{/* A selector term that matches one of the platform's pods under either value of its name label. */}}
{{- define "tranibor.anyPodName" -}}
{ key: app.kubernetes.io/name, operator: In, values: [tranibor-{{ . }}, choregos-{{ . }}] }
{{- end -}}

{{/*
An rclone S3 remote named by environment variables (`RCLONE_CONFIG_<REMOTE>_*`), for the backup
(ADR 0184): no configuration file, the keys read from a Secret with `access-key` and `secret-key`.
An empty endpoint means AWS; any other is an S3-compatible store addressed by path.
*/}}
{{- define "tranibor.backupRemote" -}}
- { name: RCLONE_CONFIG_{{ .remote }}_TYPE, value: s3 }
- { name: RCLONE_CONFIG_{{ .remote }}_PROVIDER, value: {{ ternary "Other" "AWS" (not (empty .endpoint)) }} }
- { name: RCLONE_CONFIG_{{ .remote }}_REGION, value: {{ .region | default "us-east-1" | quote }} }
{{- with .endpoint }}
- { name: RCLONE_CONFIG_{{ $.remote }}_ENDPOINT, value: {{ . | quote }} }
- { name: RCLONE_CONFIG_{{ $.remote }}_FORCE_PATH_STYLE, value: "true" }
{{- end }}
{{- if .secretRef }}
- name: RCLONE_CONFIG_{{ .remote }}_ACCESS_KEY_ID
  valueFrom: { secretKeyRef: { name: {{ .secretRef }}, key: access-key } }
- name: RCLONE_CONFIG_{{ .remote }}_SECRET_ACCESS_KEY
  valueFrom: { secretKeyRef: { name: {{ .secretRef }}, key: secret-key } }
{{- else }}
- { name: RCLONE_CONFIG_{{ .remote }}_ENV_AUTH, value: "true" }
{{- end }}
{{- end -}}

{{/*
The environment of the backup's and the restore's `memory` steps (S80-41, #2185): the embedded
Ecphoria's URL and the key its admin API takes, the platform's own (`<release>-memory`, an admin key
with no tenant, unless `global.memory.tokenSecret` names another one, which must be an admin key too).
*/}}
{{- define "tranibor.backupMemoryEnv" -}}
{{- toYaml .env }}
- { name: MEMORY_URL, value: {{ include "tranibor.memoryUrl" .root | quote }} }
- name: MEMORY_TOKEN
  valueFrom: { secretKeyRef: { name: {{ include "tranibor.memoryTokenSecret" .root }}, key: token } }
{{- end -}}

{{- define "tranibor.labels" -}}
app.kubernetes.io/name: {{ include "tranibor.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ include "tranibor.labelPrefix" . }}
{{- end -}}

{{/*
Image reference: by digest as soon as `global.imageDigest` is provided (Kyverno requirement).

A component can come from ANOTHER registry (`image.registry`): the enterprise edition serves its
API and orchestrator images from a private project, the rest comes from the core (ADR 0032,
S17-03). An image from another registry is another image: the global digest does not designate it,
only its `image.digest` or its tag count.
*/}}
{{- define "tranibor.image" -}}
{{- $global := .global -}}
{{- $ailleurs := and .image.registry (ne .image.registry $global.imageRegistry) -}}
{{- $repo := printf "%s/%s" (default $global.imageRegistry .image.registry) .image.repository -}}
{{- if .image.digest -}}
{{ $repo }}@{{ .image.digest }}
{{- else if and $global.imageDigest (not $ailleurs) -}}
{{ $repo }}@{{ $global.imageDigest }}
{{- else -}}
{{ $repo }}:{{ default $global.imageTag .image.tag }}
{{- end -}}
{{- end -}}

{{/*
The executor that runs agent stages (ADR 0072), one answer for every pod. The API needs it as
much as the workers: it showed "tekton" on the project settings page while the workers ran
`k8s_job`, because only the workers received the variable. `global.runner.executor` is the
setting; the orchestrator sub-chart's own `runner.executor` may only repeat it (a different value
there would bring back the two answers, so the render refuses it).
*/}}
{{- define "tranibor.executorKind" -}}
{{- $global := (.Values.global.runner | default dict).executor | default "agent_sandbox" -}}
{{- $local := "" -}}
{{- with .Values.runner }}{{- $local = .executor | default "" -}}{{- end -}}
{{- if and $local (ne $local $global) -}}
{{- fail (printf "runner.executor=%s on the orchestrator differs from global.runner.executor=%s. Set the executor once, in global.runner.executor: the API and the workers must agree on it." $local $global) -}}
{{- end -}}
{{- $global -}}
{{- end -}}

{{/*
The RuntimeClass of the warm pool's template (gVisor, Kata). `global.runner.runtimeClass` is the
setting that the API also reads (S38-13d): when it is SET (even to "", the cluster's default), the API
and every worker receive it and `/readiness` compares it with what a project's policy asks. When it
is NEVER set, the orchestrator sub-chart's own `runner.runtimeClass` still applies to the pool as
before, but the API is told nothing and treats the pool's class as unknown (no judgment, no false
alarm). Both set and different: refused, that is a contradiction.
*/}}
{{- define "tranibor.runtimeClass" -}}
{{- $runner := .Values.global.runner | default dict -}}
{{- $local := "" -}}
{{- with .Values.runner }}{{- $local = .runtimeClass | default "" -}}{{- end -}}
{{- if hasKey $runner "runtimeClass" -}}
{{- $global := $runner.runtimeClass | default "" -}}
{{- if and $local (ne $local $global) -}}
{{- fail (printf "runner.runtimeClass=%s on the orchestrator differs from global.runner.runtimeClass=%q: set the RuntimeClass once, in global.runner.runtimeClass." $local $global) -}}
{{- end -}}
{{- $global -}}
{{- else -}}
{{- $local -}}
{{- end -}}
{{- end -}}

{{/*
Where every run's agent runs (ADR 0146): `container` (the default since S65-04m) or `process`. One value
for the API (`/readiness`),
the workers (each run's executor) and the warm pool's template, as `TRANIBOR_RUNNER_AGENT_ISOLATION`.
`runner.sandbox.agentIsolation`, where it lived while only the warm pool had the agent's container, is
refused with where it went: a value silently ignored would leave the agent beside the runner.
*/}}
{{- define "tranibor.agentIsolation" -}}
{{- $value := (.Values.global.runner | default dict).agentIsolation | default "container" -}}
{{- if not (has $value (list "process" "container")) -}}
{{- fail (printf "global.runner.agentIsolation=%q: expected process or container (ADR 0146)" $value) -}}
{{- end -}}
{{- with .Values.runner -}}{{- with .sandbox -}}{{- if hasKey . "agentIsolation" -}}
{{- fail "tranibor-orchestrator.runner.sandbox.agentIsolation moved to global.runner.agentIsolation (S65-04d): one setting for every executor, the API and the workers; set it there and remove this one" -}}
{{- end -}}{{- end -}}{{- end -}}
{{- if and (eq $value "process") (has (.Values.global.environment | default "dev") (list "staging" "prod")) (not ((.Values.global.runner | default dict).acceptUnisolatedAgent | default false)) -}}
{{- fail (printf "global.runner.agentIsolation=process with global.environment=%s: the agent would run in the runner's container and read its run token, git token and egress report key (ADR 0146). Use `container` (k8s_job, agent_sandbox, local_docker, tekton), or accept the risk by name with global.runner.acceptUnisolatedAgent: true (TRANIBOR_AGENT_ISOLATION=none-accepted, logged on every run, ADR 0222)." .Values.global.environment) -}}
{{- end -}}
{{- $value -}}
{{- end -}}

{{/*
Whether the operator named the opt-out of ADR 0222: an agent beside the runner's credentials, in staging or
prod, accepted. Only meaningful under `process`; rendered as `TRANIBOR_AGENT_ISOLATION=none-accepted`.
*/}}
{{- define "tranibor.unisolatedAgentAccepted" -}}
{{- if and (eq (include "tranibor.agentIsolation" .) "process") ((.Values.global.runner | default dict).acceptUnisolatedAgent | default false) -}}true{{- end -}}
{{- end -}}

{{/*
The namespace pattern the runs go to. Empty (the default) follows the executor: the release
namespace for `agent_sandbox`, whose SandboxTemplate lives there, `proj-{slug}-runners` otherwise.
An explicit value always wins.
*/}}
{{- define "tranibor.runnerNamespacePattern" -}}
{{- if .Values.runner.namespacePattern -}}
{{- .Values.runner.namespacePattern -}}
{{- else if eq (include "tranibor.executorKind" .) "agent_sandbox" -}}
{{- .Release.Namespace -}}
{{- else -}}
proj-{slug}-runners
{{- end -}}
{{- end -}}

{{/*
The name a process reads for a variable of `global.extraEnv` (ADR 0226, R9b). The chart renders the
platform's variables as `TRANIBOR_*` since 0.30.0; a values file may still set one under its former name,
`CHOREGOS_X`, which the processes have read as `TRANIBOR_X` since 0.27.17 (ADR 0176 R4), `TRANIBOR_X`
winning when both are set. An entry is rendered under the name it was written with (an extension may read
`CHOREGOS_X` itself) and compared under this one (`tranibor.extraEnvChecked`).
*/}}
{{- define "tranibor.envName" -}}
{{- if hasPrefix "CHOREGOS_" . -}}
{{- printf "TRANIBOR_%s" (trimPrefix "CHOREGOS_" .) -}}
{{- else -}}
{{- . -}}
{{- end -}}
{{- end -}}

{{/* `X (the former name of TRANIBOR_X)` for a former name, the name alone otherwise: what a refusal says. */}}
{{- define "tranibor.envNameSaid" -}}
{{- $new := include "tranibor.envName" . -}}
{{- if eq $new . -}}{{ . }}{{- else -}}{{ printf "%s (the former name of %s)" . $new }}{{- end -}}
{{- end -}}

{{/*
The checks on a list of extra variables (`global.extraEnv`, `global.outposts.door.extraEnv`):
`(dict "entries" <list> "owned" <names the chart sets> "key" <the values key> "by" <who sets them>)`.
- A name the chart sets is refused, under its former name too: `CHOREGOS_X` beside the chart's `TRANIBOR_X`
  would be shadowed by it without a word (ADR 0226, R9b). Before 0.30.0 the chart set `CHOREGOS_X`, so the
  same entry was refused already: nothing that rendered is refused now.
- `CHOREGOS_X` and `TRANIBOR_X` both listed with different values are two values for one variable, of which
  the process would keep `TRANIBOR_X` without a word: refused. With the same value they are harmless.
- `value` or `valueFrom`, exactly one (S38-18): an entry with neither rendered `value: ""`, a key that
  reached every pod empty without anything saying so.
*/}}
{{- define "tranibor.extraEnvChecked" -}}
{{- $seen := dict -}}
{{- range .entries -}}
{{- $name := include "tranibor.envName" .name -}}
{{- if has $name $.owned -}}
{{- fail (printf "%s: %s is already set by %s; two values for the same name, Kubernetes would keep the last one without saying anything" $.key (include "tranibor.envNameSaid" .name) $.by) -}}
{{- end -}}
{{- if eq (hasKey . "value") (hasKey . "valueFrom") -}}
{{- fail (printf "%s: %s needs exactly one of value or valueFrom" $.key .name) -}}
{{- end -}}
{{- $given := toJson (pick . "value" "valueFrom") -}}
{{- if hasKey $seen $name -}}
{{- $other := get $seen $name -}}
{{- if ne (get $other "given") $given -}}
{{- fail (printf "%s: %s and %s are one variable with two values (ADR 0226: CHOREGOS_X is read as TRANIBOR_X, and TRANIBOR_X wins); keep %s" $.key (get $other "name") .name $name) -}}
{{- end -}}
{{- end -}}
{{- $_ := set $seen $name (dict "name" .name "given" $given) -}}
{{- end -}}
{{- end -}}

{{/* Common environment variables: 12-factor configuration, secrets by reference. */}}
{{- define "tranibor.commonEnv" -}}
{{- $poses := list "TRANIBOR_ENV" "TRANIBOR_EDITION" "TRANIBOR_LOG_LEVEL" "TRANIBOR_FAKES" "TRANIBOR_EGRESS_MODE" "TRANIBOR_EGRESS_CEILING" "TRANIBOR_EGRESS_PLATFORM_CHANGE" "TRANIBOR_TEMPORAL_ADDRESS" "TRANIBOR_TEMPORAL_NAMESPACE" "TRANIBOR_GATEWAY_URL" "TRANIBOR_MEMORY_URL" "TRANIBOR_MEMORY_TOKEN" "TRANIBOR_OBJECT_STORE_KIND" "TRANIBOR_OBJECT_STORE_URL" "TRANIBOR_OBJECT_STORE_ENDPOINT" "TRANIBOR_OBJECT_STORE_REGION" "TRANIBOR_OBJECT_STORE_ACCESS_KEY" "TRANIBOR_OBJECT_STORE_SECRET_KEY" "TRANIBOR_PUBLIC_URL" "TRANIBOR_API_URL" "TRANIBOR_DB_POOL_SIZE" "TRANIBOR_DB_MAX_OVERFLOW" "TRANIBOR_DB_POOL_TIMEOUT_S" "TRANIBOR_DATABASE_PASSWORD" "TRANIBOR_DATABASE_URL" "TRANIBOR_SECRET_ENV_PATTERN" "TRANIBOR_EXECUTOR_KIND" "TRANIBOR_RUNNER_RUNTIME_CLASS" "TRANIBOR_EXECUTORS_ALLOWED" "TRANIBOR_RUNNER_AGENT_ISOLATION" "TRANIBOR_DNS_POLICY" }}
{{- if include "tranibor.unisolatedAgentAccepted" . }}
{{- $poses = append $poses "TRANIBOR_AGENT_ISOLATION" }}
{{- end }}
{{- if and (eq (include "tranibor.objectStoreKind" .) "azure-blob") .Values.global.objectStore.secretRef }}
{{- $poses = concat $poses (list "TRANIBOR_OBJECT_STORE_ACCOUNT_KEY" "TRANIBOR_OBJECT_STORE_SAS_TOKEN") }}
{{- end }}
{{- /* Before S40-20b the endpoint went through `extraEnv`: still accepted, unless the chart sets it. */}}
{{- if (.Values.global.tracing | default dict).otlpEndpoint }}
{{- $poses = append $poses "OTEL_EXPORTER_OTLP_ENDPOINT" }}
{{- end }}
{{- $gw := .Values.global.mcpGateway | default dict }}
{{- if $gw.internalHosts }}
{{- $poses = append $poses "TRANIBOR_MCP_GATEWAY_INTERNAL_HOSTS" }}
{{- end }}
{{- /* Before S65-16 the outposts' settings went through `extraEnv` too: still accepted while
       `global.outposts.enabled` is false, refused once the chart sets them. */}}
{{- if include "tranibor.outpostsEnabled" . }}
{{- $poses = concat $poses (splitList " " (include "tranibor.outpostsOwnedEnv" "common")) }}
{{- end }}
{{- include "tranibor.extraEnvChecked" (dict "entries" (.Values.global.extraEnv | default list) "owned" $poses "key" "global.extraEnv" "by" "the chart") }}
- name: TRANIBOR_ENV
  value: {{ .Values.global.environment | quote }}
- name: TRANIBOR_EDITION
  value: {{ .Values.global.edition | default "community" | quote }}
- name: TRANIBOR_LOG_LEVEL
  value: {{ .Values.global.logLevel | quote }}
- name: TRANIBOR_FAKES
  value: {{ .Values.global.fakes | quote }}
{{- $egress := .Values.global.egress | default dict }}
- name: TRANIBOR_EGRESS_MODE
  value: {{ $egress.mode | default "enforce" | quote }}
{{- /* ADR 0105: the gateway hosts join the families the organisation declared, never `runners`. */}}
{{- $ceiling := deepCopy ($egress.ceiling | default dict) }}
{{- range $family := list "api" "workers" }}
{{- if hasKey $ceiling $family }}
{{- $_ := set $ceiling $family (uniq (concat (get $ceiling $family) ($gw.hosts | default list))) }}
{{- end }}
{{- end }}
{{- if $ceiling }}
- name: TRANIBOR_EGRESS_CEILING
  value: {{ toJson $ceiling | quote }}
{{- end }}
{{- if $gw.internalHosts }}
- name: TRANIBOR_MCP_GATEWAY_INTERNAL_HOSTS
  value: {{ toJson $gw.internalHosts | quote }}
{{- end }}
{{- with $egress.platformChange }}
- name: TRANIBOR_EGRESS_PLATFORM_CHANGE
  value: {{ toJson . | quote }}
{{- end }}
- name: TRANIBOR_TEMPORAL_ADDRESS
  value: {{ include "tranibor.temporalAddress" . | quote }}
- name: TRANIBOR_TEMPORAL_NAMESPACE
  value: {{ .Values.global.temporal.namespace | quote }}
- name: TRANIBOR_EXECUTOR_KIND
  value: {{ include "tranibor.executorKind" . | quote }}
{{- if hasKey (.Values.global.runner | default dict) "runtimeClass" }}
# The warm pool's RuntimeClass, for the API and the workers alike (empty: the cluster's default).
# Absent when `global.runner.runtimeClass` is never set: the API then knows nothing.
- name: TRANIBOR_RUNNER_RUNTIME_CLASS
  value: {{ include "tranibor.runtimeClass" . | quote }}
{{- end }}
- name: TRANIBOR_EXECUTORS_ALLOWED
  value: {{ (.Values.global.runner | default dict).allowedExecutors | default list | join "," | quote }}
# Where every run's agent runs (ADR 0146): the API judges `/readiness` with it, the workers hand it to
# each run's executor, which refuses a run it cannot isolate.
- name: TRANIBOR_RUNNER_AGENT_ISOLATION
  value: {{ include "tranibor.agentIsolation" . | quote }}
{{- if include "tranibor.unisolatedAgentAccepted" . }}
# The named opt-out (ADR 0222): `process` in staging or prod, accepted. The workers log it on every run,
# `/readiness` shows it, and the runner finds it beside its `TRANIBOR_ENV`.
- name: TRANIBOR_AGENT_ISOLATION
  value: none-accepted
{{- end }}
{{- with (.Values.global.tracing | default dict).otlpEndpoint }}
# ADR 0018: the collector the traces go to; absent, `configure_tracing` leaves tracing off.
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ . | quote }}
{{- end }}
- name: TRANIBOR_GATEWAY_TYPE
  value: {{ .Values.global.gateway.type | default "litellm" | quote }}
- name: TRANIBOR_GATEWAY_URL
  value: {{ include "tranibor.gatewayUrl" . | quote }}
- name: TRANIBOR_MEMORY_URL
  value: {{ include "tranibor.memoryUrl" . | quote }}
{{- with (include "tranibor.memoryTokenSecret" .) }}
- name: TRANIBOR_MEMORY_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: token
{{- end }}
{{- $storeKind := include "tranibor.objectStoreKind" . }}
- name: TRANIBOR_OBJECT_STORE_KIND
  value: {{ $storeKind | quote }}
- name: TRANIBOR_OBJECT_STORE_URL
  value: {{ .Values.global.objectStore.url | quote }}
- name: TRANIBOR_OBJECT_STORE_ENDPOINT
  value: {{ include "tranibor.objectStoreEndpoint" . | quote }}
- name: TRANIBOR_OBJECT_STORE_REGION
  value: {{ .Values.global.objectStore.region | default "us-east-1" | quote }}
{{- if eq $storeKind "azure-blob" }}
{{- with .Values.global.objectStore.secretRef }}
# Azure Blob with a key (a bench): the account key or a SAS token, whichever the Secret holds.
- name: TRANIBOR_OBJECT_STORE_ACCOUNT_KEY
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: account-key
      optional: true
- name: TRANIBOR_OBJECT_STORE_SAS_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: sas-token
      optional: true
{{- end }}
{{- else }}
{{- with .Values.global.objectStore.secretRef }}
- name: TRANIBOR_OBJECT_STORE_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: access-key
- name: TRANIBOR_OBJECT_STORE_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: secret-key
{{- end }}
{{- end }}
- name: TRANIBOR_PUBLIC_URL
  value: {{ .Values.global.publicUrl | default (printf "https://app.%s" .Values.global.domain) | quote }}
- name: TRANIBOR_API_URL
  value: {{ .Values.global.apiUrl | default (printf "https://api.%s" .Values.global.domain) | quote }}
{{- /* The connection pool, BOUNDED: `pool_size` alone let SQLAlchemy open ten
       overflow connections per process with no wait-time limit (status review
       of 2026-09-24). Each API replica and each worker holds at most size+maxOverflow
       connections; multiply by the replicas to size `max_connections`. */}}
- name: TRANIBOR_DB_POOL_SIZE
  value: {{ .Values.global.database.pool.size | quote }}
- name: TRANIBOR_DB_MAX_OVERFLOW
  value: {{ .Values.global.database.pool.maxOverflow | quote }}
- name: TRANIBOR_DB_POOL_TIMEOUT_S
  value: {{ .Values.global.database.pool.timeoutSeconds | quote }}
{{- include "tranibor.databaseEnv" . }}
{{- with (.Values.global.secrets | default dict).envPattern }}
- name: TRANIBOR_SECRET_ENV_PATTERN
  value: {{ . | quote }}
{{- end }}
{{- if include "tranibor.outpostsEnabled" . }}
{{- /* Outposts (ADR 0147 §12): the switch and the quotas of calls in flight, for whoever places a
       call: the API for a read, the workers for a governed write (ADR 0149 §4-5). */}}
{{ include "tranibor.outpostsEnv" . }}
{{- end }}
{{- range (.Values.global.extraEnv | default list) }}
- name: {{ .name }}
{{- if hasKey . "valueFrom" }}
  valueFrom:
    {{- toYaml .valueFrom | nindent 4 }}
{{- else }}
  value: {{ .value | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
Where the database is, as a process reads it (`TRANIBOR_DATABASE_URL`). Its own template: the
outposts' door served alone (`templates/outposts-door.yaml`) takes it without the rest of
`choregos.commonEnv`.
*/}}
{{- define "tranibor.databaseEnv" -}}
{{- with (include "tranibor.databasePasswordSecret" .) }}
{{- /* The password comes from a secret laid down by an operator (Zalando: `password` key),
       the URL is composed here — Kubernetes expands `$(VAR)` of a variable declared earlier. */}}
- name: TRANIBOR_DATABASE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: password
- name: TRANIBOR_DATABASE_URL
  {{- /* Embedded: the application role without SUPERUSER created at initdb. External: the role
         the operator created (Zalando, CNPG), already without SUPERUSER — `choregos_app` does not exist there. */}}
  value: {{ printf "postgresql+asyncpg://%s:$(TRANIBOR_DATABASE_PASSWORD)@%s:%v/%s" (ternary ($.Values.global.database.appUser | default $.Values.global.database.user) $.Values.global.database.user $.Values.global.database.embedded) (include "tranibor.databaseHost" $) $.Values.global.database.port $.Values.global.database.name | quote }}
{{- else }}
- name: TRANIBOR_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.global.database.secretRef }}
      key: url
{{- end }}
{{- end -}}

{{/*
Outposts (ADRs 0147-0149). `global.outposts` is read by the API's subchart, by the workers' and by
this chart's own templates: these helpers give them one answer.

`choregos.outpostsEnabled`: "true" when the switch is on, empty otherwise.
*/}}
{{- define "tranibor.outpostsEnabled" -}}
{{- if eq (toString (.Values.global.outposts | default dict).enabled) "true" -}}
true
{{- end -}}
{{- end -}}

{{/*
The door's public origin (`TRANIBOR_OUTPOSTS_DOOR_URL`): `door.url`, else `https://` and the first of
the door's hostnames. The API refuses to start with outposts on and no origin, and on one that has
a path: the render refuses first, and names the value.
*/}}
{{- define "tranibor.outpostsDoorUrl" -}}
{{- $door := (.Values.global.outposts | default dict).door | default dict -}}
{{- $hosts := compact ($door.hostnames | default list) -}}
{{- $url := $door.url | default "" -}}
{{- if and (not $url) (not $hosts) -}}
{{- fail "global.outposts.enabled=true needs the door's public origin: set global.outposts.door.hostnames (the door's own hostnames; the origin is https://<the first one>) or global.outposts.door.url (https://outposts.example.com). An outpost signs the URL it calls, and the API refuses to start without the origin it compares it with (TRANIBOR_OUTPOSTS_DOOR_URL)." -}}
{{- end -}}
{{- if not $url -}}{{- $url = printf "https://%s" (first $hosts) -}}{{- end -}}
{{- if not (regexMatch "^https?://[^\\s/@?#]+$" $url) -}}
{{- fail (printf "global.outposts.door.url: %q is not an origin: a scheme and a host, no path (https://outposts.example.com). The API refuses to start on it." $url) -}}
{{- end -}}
{{- $url -}}
{{- end -}}

{{/*
The Secret that holds the door's signing key: the one the operator names, else the one this chart
draws once and keeps (`templates/outposts-door.yaml`).
*/}}
{{- define "tranibor.outpostsDoorKeySecret" -}}
{{- $key := ((.Values.global.outposts | default dict).door | default dict).signingKey | default dict -}}
{{- $key.existingSecret | default (printf "%s-outposts-door" .Release.Name) -}}
{{- end -}}

{{/*
What whoever places a call through an outpost reads: the switch, and the two quotas of calls in
flight (`outposts/rendezvous.py`, `place`). Part of `choregos.commonEnv` when outposts are on.
*/}}
{{- define "tranibor.outpostsEnv" -}}
{{- $flight := (.Values.global.outposts | default dict).callsInFlight | default dict -}}
- name: TRANIBOR_OUTPOSTS
  value: "on"
- name: TRANIBOR_OUTPOSTS_CALLS_IN_FLIGHT_PER_OUTPOST
  value: {{ int64 ($flight.perOutpost | default 128) | quote }}
- name: TRANIBOR_OUTPOSTS_CALLS_IN_FLIGHT_PER_ORGANISATION
  value: {{ int64 ($flight.perOrganisation | default 512) | quote }}
{{- end -}}

{{/*
What the processes that serve the door or answer for it read, and no other: the API (it serves the
door unless it is split out, and it shows the keys to pin with an outpost and its enrolment token)
and the door's own Deployment. Never the workers: they sign no call. The signing key is read from
its Secret by reference, and is not optional: a door that could sign nothing would hand nothing over.
*/}}
{{- define "tranibor.outpostsDoorEnv" -}}
{{- $door := (.Values.global.outposts | default dict).door | default dict -}}
{{- $key := $door.signingKey | default dict -}}
{{- $announced := $door.announcedKeys | default list -}}
{{- range $announced -}}
{{- if not (regexMatch "^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$" (toString .)) -}}
{{- fail (printf "global.outposts.door.announcedKeys: %q is not a public key: the unpadded base64url of its 32 raw bytes (43 characters), as the management API shows the door's keys. The API refuses to start on it." (toString .)) -}}
{{- end -}}
{{- end -}}
{{- if gt (len $announced) 3 -}}
{{- fail "global.outposts.door.announcedKeys: at most three keys beside the one that signs (an outpost's policy file pins four). The API refuses to start on more." -}}
{{- end -}}
{{- if ne (len (uniq $announced)) (len $announced) -}}
{{- fail "global.outposts.door.announcedKeys: a key is announced once. The API refuses to start on a key listed twice." -}}
{{- end -}}
- name: TRANIBOR_OUTPOSTS_DOOR_URL
  value: {{ include "tranibor.outpostsDoorUrl" . | quote }}
- name: TRANIBOR_OUTPOSTS_DOOR_SIGNING_KEY
  valueFrom: { secretKeyRef: { name: {{ include "tranibor.outpostsDoorKeySecret" . }}, key: {{ $key.key | default "signing-key" }} } }
- name: TRANIBOR_OUTPOSTS_DOOR_ANNOUNCED_KEYS
  value: {{ toJson $announced | quote }}
- name: TRANIBOR_OUTPOSTS_DOOR_RATE_PER_MINUTE
  value: {{ if hasKey $door "ratePerMinute" }}{{ int64 $door.ratePerMinute | quote }}{{ else }}"1200"{{ end }}
{{- end -}}

{{/*
The SIEM export of the audit log (ADR 0207): what the API is told when `global.auditExport.enabled`. The
token comes from a Secret by reference (`secretKeyRef`), never a value; the API refuses to start on a sink
it cannot build, so the mistakes the chart can see are refused here, at render time.
*/}}
{{- define "tranibor.auditExportEnv" -}}
{{- $x := .Values.global.auditExport | default dict -}}
{{- $sink := $x.sink | default "https" -}}
{{- if not (has $sink (list "https" "syslog")) -}}
{{- fail (printf "global.auditExport.sink: %q is not https or syslog" $sink) -}}
{{- end -}}
{{- $format := $x.format | default (ternary "webhook" "rfc5424" (eq $sink "https")) -}}
{{- if not (has $format (ternary (list "webhook" "splunk-hec" "elastic-bulk") (list "rfc5424" "cef") (eq $sink "https"))) -}}
{{- fail (printf "global.auditExport.format: %q does not fit the %s sink (https: webhook, splunk-hec, elastic-bulk; syslog: rfc5424, cef)" $format $sink) -}}
{{- end -}}
{{- if and (eq $sink "https") (not (hasPrefix "https://" (toString ($x.url | default "")))) -}}
{{- fail "global.auditExport.url: an https:// address is required (the log is not sent in clear)" -}}
{{- end -}}
{{- if and (eq $sink "syslog") (not (($x.syslog | default dict).host)) -}}
{{- fail "global.auditExport.syslog.host: required for the syslog sink" -}}
{{- end -}}
{{- $token := $x.token | default dict -}}
- name: TRANIBOR_AUDIT_EXPORT
  value: "true"
- name: TRANIBOR_AUDIT_EXPORT_SINK
  value: {{ $sink | quote }}
- name: TRANIBOR_AUDIT_EXPORT_NAME
  value: {{ $x.name | default "siem" | quote }}
- name: TRANIBOR_AUDIT_EXPORT_FORMAT
  value: {{ $format | quote }}
{{- if eq $sink "https" }}
- name: TRANIBOR_AUDIT_EXPORT_URL
  value: {{ $x.url | quote }}
- name: TRANIBOR_AUDIT_EXPORT_HEADER
  value: {{ $token.header | default "Authorization" | quote }}
- name: TRANIBOR_AUDIT_EXPORT_SCHEME
  value: {{ if hasKey $token "scheme" }}{{ $token.scheme | quote }}{{ else }}"Bearer"{{ end }}
{{- with $x.index }}
- name: TRANIBOR_AUDIT_EXPORT_INDEX
  value: {{ . | quote }}
{{- end }}
{{- else }}
- name: TRANIBOR_AUDIT_EXPORT_SYSLOG_HOST
  value: {{ $x.syslog.host | quote }}
- name: TRANIBOR_AUDIT_EXPORT_SYSLOG_PORT
  value: {{ int (($x.syslog).port | default 6514) | quote }}
{{- end }}
{{- with $token.secretRef }}
- name: TRANIBOR_AUDIT_EXPORT_TOKEN
  valueFrom: { secretKeyRef: { name: {{ . | quote }}, key: {{ $token.key | default "audit-export-token" | quote }} } }
{{- end }}
{{- with $x.caFile }}
- name: TRANIBOR_AUDIT_EXPORT_CA_FILE
  value: {{ . | quote }}
{{- end }}
- name: TRANIBOR_AUDIT_EXPORT_ORGS
  value: {{ toJson ($x.organisations | default list) | quote }}
- name: TRANIBOR_AUDIT_EXPORT_PLATFORM
  value: {{ ne (toString $x.platform) "false" | quote }}
- name: TRANIBOR_AUDIT_EXPORT_CANONICAL
  value: {{ ne (toString $x.canonical) "false" | quote }}
- name: TRANIBOR_AUDIT_EXPORT_SOURCE
  value: {{ $x.source | default "choregos" | quote }}
- name: TRANIBOR_AUDIT_EXPORT_INTERVAL_S
  value: {{ $x.intervalSeconds | default 10 | quote }}
- name: TRANIBOR_AUDIT_EXPORT_BATCH
  value: {{ int ($x.batch | default 500) | quote }}
{{- end -}}

{{/*
The names `choregos.auditExportEnv` sets: `global.extraEnv` may not set them a second time.
*/}}
{{- define "tranibor.auditExportOwnedEnv" -}}
TRANIBOR_AUDIT_EXPORT TRANIBOR_AUDIT_EXPORT_SINK TRANIBOR_AUDIT_EXPORT_NAME TRANIBOR_AUDIT_EXPORT_FORMAT TRANIBOR_AUDIT_EXPORT_URL TRANIBOR_AUDIT_EXPORT_HEADER TRANIBOR_AUDIT_EXPORT_SCHEME TRANIBOR_AUDIT_EXPORT_INDEX TRANIBOR_AUDIT_EXPORT_SYSLOG_HOST TRANIBOR_AUDIT_EXPORT_SYSLOG_PORT TRANIBOR_AUDIT_EXPORT_TOKEN TRANIBOR_AUDIT_EXPORT_CA_FILE TRANIBOR_AUDIT_EXPORT_ORGS TRANIBOR_AUDIT_EXPORT_PLATFORM TRANIBOR_AUDIT_EXPORT_CANONICAL TRANIBOR_AUDIT_EXPORT_SOURCE TRANIBOR_AUDIT_EXPORT_INTERVAL_S TRANIBOR_AUDIT_EXPORT_BATCH
{{- end -}}

{{/*
The names the two templates above set: `global.extraEnv` may not set them a second time while
outposts are on. `only` = "all" (what the API receives) or "common" (what every process receives).
*/}}
{{- define "tranibor.outpostsOwnedEnv" -}}
TRANIBOR_OUTPOSTS TRANIBOR_OUTPOSTS_CALLS_IN_FLIGHT_PER_OUTPOST TRANIBOR_OUTPOSTS_CALLS_IN_FLIGHT_PER_ORGANISATION
{{- if eq . "all" }} TRANIBOR_OUTPOSTS_DOOR_URL TRANIBOR_OUTPOSTS_DOOR_SIGNING_KEY TRANIBOR_OUTPOSTS_DOOR_ANNOUNCED_KEYS TRANIBOR_OUTPOSTS_DOOR_RATE_PER_MINUTE{{ end }}
{{- end -}}

{{/*
Dependencies: embedded by this chart, or provided from outside.

`global.<dep>.embedded` is the sub-chart's condition AND what these helpers read. The
flag lives under `global` because a sub-chart sees ONLY the parent's `global`: set
elsewhere, `charts/litellm` would not know which database host to use, and the rendering would be
right — in the parent chart only.
*/}}
{{- define "tranibor.databaseHost" -}}
{{- if .Values.global.database.embedded -}}
{{ .Release.Name }}-postgresql
{{- else -}}
{{ .Values.global.database.host }}
{{- end -}}
{{- end -}}

{{- define "tranibor.databasePasswordSecret" -}}
{{- if .Values.global.database.embedded -}}
{{ .Release.Name }}-postgresql
{{- else -}}
{{ .Values.global.database.passwordSecret }}
{{- end -}}
{{- end -}}

{{- /* The embedded Temporal's Service name: `global.temporal.serviceName` (global, so the API and the
workers, which render the address from their own values, see it too), or `<release>-temporal`. */ -}}
{{- define "tranibor.temporalServiceName" -}}
{{- (.Values.global.temporal | default dict).serviceName | default (printf "%s-temporal" .Release.Name) -}}
{{- end -}}

{{- /* The claim the embedded Temporal's SQLite file lives on: `persistence.existingClaim` when the
operator adopts a volume, otherwise the Service name. One definition for the subchart's Deployment
and the umbrella's backup and restore, so the backup mounts the claim the server writes (#2153).
Called with `(dict "root" <a context with .Values.global and .Release> "persistence" <the
tranibor-temporal persistence values>)`: the subchart passes `.Values.persistence`, the umbrella
`(index .Values "temporal").persistence`. */ -}}
{{- define "tranibor.temporalClaim" -}}
{{- (.persistence | default dict).existingClaim | default (include "tranibor.temporalServiceName" .root) -}}
{{- end -}}

{{- define "tranibor.temporalAddress" -}}
{{- if .Values.global.temporal.embedded -}}
{{ include "tranibor.temporalServiceName" . }}:7233
{{- else -}}
{{ .Values.global.temporal.address }}
{{- end -}}
{{- end -}}

{{/*
Temporal over TLS and its payload codec (ADR 0183), for the API and the workers: every process that
holds a Temporal client must hold the SAME converter, or one side writes what the other cannot read.
TLS is off by default (dev, kind, compose); the codec is on by default (ADR 0220). Three templates, one switch each: the variables, the mounts and
the volumes, so a pod that gets the variables always gets the files they name.
*/}}
{{/*
A read-only mount of the chart's under `/etc/tranibor/` (ADR 0226, R9b), and the same volume at its former
path under `/etc/choregos/` until 0.32.0: the variables the chart renders name the new path, but a path an
operator wrote (a plugins configuration's `key: /etc/choregos/plugins/…`, a variable of `extraEnv`) names the
former one and must still find its file. A mount path a values file chose is its own: no second mount.
*/}}
{{- define "tranibor.etcMount" -}}
- { name: {{ .name }}, mountPath: {{ .path }}, readOnly: true }
{{- if hasPrefix "/etc/tranibor/" .path }}
- { name: {{ .name }}, mountPath: {{ printf "/etc/choregos/%s" (trimPrefix "/etc/tranibor/" .path) }}, readOnly: true }
{{- end }}
{{- end -}}

{{- define "tranibor.temporalTlsDir" -}}/etc/tranibor/temporal-tls{{- end -}}
{{- define "tranibor.temporalCodecDir" -}}/etc/tranibor/temporal-codec{{- end -}}
{{- /* The Secret holding the master keyring: the operator's, or the one the chart draws (ADR 0220). */ -}}
{{- define "tranibor.temporalCodecSecret" -}}
{{- (((.Values.global.temporal).codec).secretName) | default (printf "%s-temporal-codec" .Release.Name) -}}
{{- end -}}

{{- define "tranibor.temporalSecurityEnv" -}}
{{- $t := .Values.global.temporal | default dict }}
{{- $tls := $t.tls | default dict }}
{{- $codec := $t.codec | default dict }}
{{- $owned := list }}
{{- if $tls.enabled }}
{{- if not $tls.secretName }}
{{- fail "global.temporal.tls.enabled needs global.temporal.tls.secretName: the Secret holding ca.crt (and tls.crt, tls.key for mutual TLS)" }}
{{- end }}
{{- $owned = concat $owned (list "TRANIBOR_TEMPORAL_TLS" "TRANIBOR_TEMPORAL_TLS_CA_FILE" "TRANIBOR_TEMPORAL_TLS_CERT_FILE" "TRANIBOR_TEMPORAL_TLS_KEY_FILE" "TRANIBOR_TEMPORAL_TLS_SERVER_NAME") }}
- name: TRANIBOR_TEMPORAL_TLS
  value: "true"
- name: TRANIBOR_TEMPORAL_TLS_CA_FILE
  value: {{ printf "%s/ca.crt" (include "tranibor.temporalTlsDir" .) | quote }}
{{- if $tls.mutual }}
- name: TRANIBOR_TEMPORAL_TLS_CERT_FILE
  value: {{ printf "%s/tls.crt" (include "tranibor.temporalTlsDir" .) | quote }}
- name: TRANIBOR_TEMPORAL_TLS_KEY_FILE
  value: {{ printf "%s/tls.key" (include "tranibor.temporalTlsDir" .) | quote }}
{{- end }}
{{- with $tls.serverName }}
- name: TRANIBOR_TEMPORAL_TLS_SERVER_NAME
  value: {{ . | quote }}
{{- end }}
{{- end }}
{{- if $codec.enabled }}
{{- $owned = concat $owned (list "TRANIBOR_TEMPORAL_CODEC_KEYS" "TRANIBOR_TEMPORAL_CODEC_KEYS_FILE") }}
- name: TRANIBOR_TEMPORAL_CODEC_KEYS_FILE
  value: {{ printf "%s/%s" (include "tranibor.temporalCodecDir" .) ($codec.key | default "keys") | quote }}
{{- else }}
{{- $production := has (.Values.global.environment | default "dev") (list "staging" "prod") }}
{{- if and $production (not $codec.allowClear) }}
{{- fail "global.temporal.codec.enabled is false in a staging or prod release: Temporal's histories would hold every ticket, result and decision in clear. Leave it on, or, knowing that clear histories are acceptable here, set global.temporal.codec.allowClear=true (ADR 0220)" }}
{{- end }}
{{- if $codec.allowClear }}
{{- $owned = concat $owned (list "TRANIBOR_TEMPORAL_ALLOW_CLEAR_PAYLOADS") }}
- name: TRANIBOR_TEMPORAL_ALLOW_CLEAR_PAYLOADS
  value: "true"
{{- end }}
{{- end }}
{{- range (.Values.global.extraEnv | default list) }}
{{- if has (include "tranibor.envName" .name) $owned }}
{{- fail (printf "global.extraEnv: %s is set by global.temporal.tls / global.temporal.codec; two values for the same name, Kubernetes would keep the last one without saying anything" (include "tranibor.envNameSaid" .name)) }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "tranibor.temporalSecurityMounts" -}}
{{- $t := .Values.global.temporal | default dict }}
{{- if ($t.tls | default dict).enabled }}
{{- include "tranibor.etcMount" (dict "name" "temporal-tls" "path" (include "tranibor.temporalTlsDir" .)) | nindent 0 }}
{{- end }}
{{- if ($t.codec | default dict).enabled }}
{{- include "tranibor.etcMount" (dict "name" "temporal-codec" "path" (include "tranibor.temporalCodecDir" .)) | nindent 0 }}
{{- end }}
{{- end -}}

{{- define "tranibor.temporalSecurityVolumes" -}}
{{- $t := .Values.global.temporal | default dict }}
{{- with ($t.tls | default dict) }}
{{- if .enabled }}
- name: temporal-tls
  secret: { secretName: {{ .secretName }} }
{{- end }}
{{- end }}
{{- with ($t.codec | default dict) }}
{{- if .enabled }}
- name: temporal-codec
  secret: { secretName: {{ include "tranibor.temporalCodecSecret" $ }} }
{{- end }}
{{- end }}
{{- end -}}

{{- /* The namespace a tracing address points into (`<service>.<namespace>.svc[...]`), so the
network policies open the collector and Tempo where they actually run (S40-20e: on one operator's
dev cluster Tempo lives in `tempo`). A host that names no namespace keeps `monitoring`, the
bootstrap's (`infra/bootstrap/otel-collector.yaml`, `tempo.yaml`). */ -}}
{{- define "tranibor.tracingNamespace" -}}
{{- $labels := splitList "." ((urlParse (. | default "http://x")).hostname | default "") -}}
{{- if and (ge (len $labels) 2) (index $labels 1) -}}
{{ index $labels 1 }}
{{- else -}}
monitoring
{{- end -}}
{{- end -}}

{{- /* S80-16: the embedded Services' names, overridable so an operator whose clients already dial another
name (`litellm`, `ecphoria`) keeps it (ADR 0195). Global, because the API, the workers and the other
subcharts render the URLs from their own values. */ -}}
{{- define "tranibor.gatewayServiceName" -}}
{{- (.Values.global.gateway).serviceName | default (printf "%s-litellm" .Release.Name) -}}
{{- end -}}
{{- define "tranibor.memoryServiceName" -}}
{{- (.Values.global.memory).serviceName | default (printf "%s-ecphoria" .Release.Name) -}}
{{- end -}}

{{- /* The claim the embedded memory's DuckDB and SQLite files live on: `persistence.existingClaim`
when the operator adopts a volume (S80-16), otherwise `<release>-ecphoria` (never the Service name,
which an adoption may change while the data stays where it is). One definition for the Ecphoria
Deployment and the umbrella's backup and restore, so the backup mounts the claim the server writes
(#2153). Called with `(dict "root" <a context with .Release> "persistence" <the tranibor-ecphoria
persistence values>)`. */ -}}
{{- define "tranibor.memoryClaim" -}}
{{- (.persistence | default dict).existingClaim | default (printf "%s-ecphoria" .root.Release.Name) -}}
{{- end -}}

{{- define "tranibor.gatewayUrl" -}}
{{- if .Values.global.gateway.embedded -}}
http://{{ include "tranibor.gatewayServiceName" . }}:4000
{{- else -}}
{{ .Values.global.gateway.url }}
{{- end -}}
{{- end -}}

{{- define "tranibor.memoryUrl" -}}
{{- if .Values.global.memory.embedded -}}
http://{{ include "tranibor.memoryServiceName" . }}:8432
{{- else -}}
{{ .Values.global.memory.url }}
{{- end -}}
{{- end -}}

{{/* Secrets: the ones you provide, or the ones the chart generates when it embeds the dependency. */}}
{{- define "tranibor.gatewaySecret" -}}
{{- if .Values.global.gateway.secretRef -}}
{{ .Values.global.gateway.secretRef }}
{{- else -}}
{{ .Release.Name }}-gateway
{{- end -}}
{{- end -}}

{{/* The secret (key `api-key`) of the decision model's bearer (ADR 0121): Laya asks for it, LiteLLM sends it. */}}
{{- define "tranibor.decisionsKeySecret" -}}
{{- if .Values.global.decisions.apiKeySecret -}}
{{ .Values.global.decisions.apiKeySecret }}
{{- else -}}
{{ .Release.Name }}-laya
{{- end -}}
{{- end -}}

{{- define "tranibor.memoryTokenSecret" -}}
{{- if .Values.global.memory.tokenSecret -}}
{{ .Values.global.memory.tokenSecret }}
{{- else if .Values.global.memory.embedded -}}
{{ .Release.Name }}-memory
{{- end -}}
{{- end -}}

{{/*
Anti-affinity between the replicas of a same component. `global.antiAffinity`: `soft`
(preferred, the default — a single node still places its pods), `hard` (required: one
replica per node, or no pod) or `none`. A PodDisruptionBudget without anti-affinity
protects against a drain, not a failure: two replicas on the same node go down together.
*/}}
{{- define "tranibor.antiAffinity" -}}
{{- $mode := .global.antiAffinity | default "soft" -}}
{{- if ne $mode "none" }}
affinity:
  podAntiAffinity:
    {{- if eq $mode "hard" }}
    requiredDuringSchedulingIgnoredDuringExecution:
      - topologyKey: kubernetes.io/hostname
        labelSelector:
          matchLabels: {{- toYaml .labels | nindent 12 }}
    {{- else }}
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
          labelSelector:
            matchLabels: {{- toYaml .labels | nindent 14 }}
    {{- end }}
{{- end }}
{{- end -}}

{{/*
The alert rules, written once and served in both flavours (PrometheusRule and VMRule).
The PromQL is the same; only the packaging changes. Since 0.21 (ADR 0098) four alerts have English
names; the `legacy_alertname` label that carried their former names was removed in 0.24.0 (S45-49).

ADR 0226 (R9b): the alerts query the `tranibor_*` series, which the API serves beside their former
`choregos_*` twins. Their names keep the former prefix (`Choregos…`): an Alertmanager route, a silence or an
inhibition matches `alertname`, a renamed alert would leave its route without a word, and the same alert
under two names would page twice. They move in a later release, after the upgrade notes have asked for
routes that match either name (`alertname=~"(Choregos|Tranibor)…"`).
*/}}
{{- define "tranibor.alertRules" -}}
- alert: ChoregosApi5xxErrors
  expr: |
    sum(rate(http_requests_total{job="tranibor-api",status=~"5.."}[5m]))
    / clamp_min(sum(rate(http_requests_total{job="tranibor-api"}[5m])), 0.001) > 0.01
  for: 10m
  labels: { severity: critical }
  annotations:
    summary: "More than 1 % of 5xx on the Tranibor API"
    runbook_url: "https://github.com/graalsystems/tranibor/blob/main/docs/runbooks/api-5xx.md"
- alert: ChoregosBacklogTemporal
  expr: sum(temporal_task_queue_backlog{namespace="default"}) > 100
  for: 10m
  labels: { severity: warning }
  annotations:
    summary: "Temporal queue saturated: stages are waiting"
- alert: ChoregosRunPending
  expr: sum(kube_pod_status_phase{phase="Pending",namespace=~"proj-.*-runners"}) > 0
  for: 15m
  labels: { severity: warning }
  annotations:
    summary: "A PipelineRun has been waiting for 15 min (autoscaling down?)"
- alert: ChoregosDailyBudgetHigh
  expr: tranibor_project_daily_cost_usd / tranibor_project_daily_budget_usd > 0.8
  for: 5m
  labels: { severity: warning }
  annotations:
    summary: "A project's daily budget is more than 80 % consumed"
- alert: ChoregosTrainFrozen
  expr: tranibor_train_frozen == 1
  for: 2h
  labels: { severity: warning }
  annotations:
    summary: "A release train has been frozen for 2 hours"
    runbook_url: "https://github.com/graalsystems/tranibor/blob/main/docs/runbooks/train-frozen.md"
{{- end }}

{{/*
The rule of the outposts' door (ADR 0148 §7), rendered with the others while outposts are on. A
second poll under a key that already has one waiting is refused by the door, which writes one audit
event a minute at most for the instance; `/metrics` counts those events per organisation. Its own
template: `choregos.alertRules` stays plain YAML, which a test reads as such.
*/}}
{{- define "tranibor.outpostsAlertRules" -}}
- alert: ChoregosOutpostPollConflict
  expr: max by (org) (increase(tranibor_outpost_poll_conflicts_total[15m])) > 0
  labels: { severity: critical }
  annotations:
    summary: "A second poll arrived under an outpost instance's key: two holders of one key is what a stolen key looks like"
    description: "Organisation {{ "{{" }} $labels.org {{ "}}" }}: the door refused the second poll (409 poll_conflict) and wrote `outpost.poll_conflict` in the organisation's audit log, which names the instance. A daemon restarted while its former poll still held the slot raises it too. If the key may be held twice, revoke the instance: its signature opens nothing from its next request."
{{- end }}

{{/*
The alert of the SIEM export (ADR 0207), rendered only while `global.auditExport.enabled`: entries are waiting
and no cursor has moved for half an hour, which a sink that is down or refusing looks like. Plain YAML, like
the other rule lists.
*/}}
{{- define "tranibor.auditExportAlertRules" -}}
- alert: ChoregosAuditExportStalled
  expr: |
    max by (sink) (tranibor_audit_export_lag_entries) > 0
    and on (sink) (time() - max by (sink) (tranibor_audit_export_progress_timestamp_seconds)) > 1800
  for: 10m
  labels: { severity: warning }
  annotations:
    summary: "The audit log is not reaching the SIEM: entries wait and no cursor has moved for 30 minutes"
    description: "Sink {{ "{{" }} $labels.sink {{ "}}" }}: the API logs `audit.export.failed` with the sink's own error for each chain it could not ship. Nothing is lost: the cursor stays and the batch is sent again."
{{- end }}

{{/*
Image pull secrets, for private registries.

The chart declared none (`rg imagePullSecrets charts/`: zero). As long as all the images
are public, nobody sees it. As soon as one no longer is — an enterprise edition, a private
Harbor mirror, a home-made image — the deployment fails with `ImagePullBackOff`, and the only way out
was to patch the service account by hand, outside GitOps.

The name is global (`global.imagePullSecrets`) because the question arises for ALL the chart's pods,
and a tenant that sets one secret sets one for the whole set.
*/}}
{{- define "tranibor.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Whether the chart deploys its own `buildkitd` (S41-13): BuildKit is the builder, there is a
repository to push to, and no other daemon is named. Reads `global` only, so the orchestrator's
subchart asks the same question and gets the same answer.
*/}}
{{- define "tranibor.buildkitdDeployed" -}}
{{- $images := .Values.global.images | default dict -}}
{{- $b := .Values.global.buildkitd | default dict -}}
{{- if and (ne $b.enabled false) (eq ($images.hostedBuilder | default "buildkit") "buildkit") $images.buildRegistry (not $images.buildkitAddress) -}}
true
{{- end -}}
{{- end -}}

{{/* The address `buildctl` dials: the operator's daemon, else the chart's, else none. */}}
{{- define "tranibor.buildkitAddress" -}}
{{- $images := .Values.global.images | default dict -}}
{{- if $images.buildkitAddress -}}
{{- $images.buildkitAddress -}}
{{- else if include "tranibor.buildkitdDeployed" . -}}
{{- printf "tcp://tranibor-buildkitd:%d" (int ((.Values.global.buildkitd | default dict).port | default 1234)) -}}
{{- end -}}
{{- end -}}

{{/*
The paths a custom host (`global.customHosts`, S74-04) sends to the API: the same as `tranibor-api`'s
own route (`charts/api/templates/httproute.yaml`), which `tests/charts/test_custom_hosts.py` compares.
Prefix paths, then the exact ones; everything else goes to the console.
*/}}
{{- define "tranibor.customHostApiPaths" -}}
["/api", "/mcp", "/healthz", "/readyz", "/.well-known/oauth-protected-resource", "/a2a", "/downloads"]
{{- end }}
{{- define "tranibor.customHostApiExactPaths" -}}
["/cli/install.sh", "/cli/install.ps1"]
{{- end }}
{{- define "tranibor.customHostApiRules" -}}
{{- range (include "tranibor.customHostApiPaths" . | fromYamlArray) }}
- path: {{ . }}
  pathType: Prefix
  backend:
    service: { name: tranibor-api, port: { number: {{ $.port }} } }
{{- end }}
{{- range (include "tranibor.customHostApiExactPaths" . | fromYamlArray) }}
- path: {{ . }}
  pathType: Exact
  backend:
    service: { name: tranibor-api, port: { number: {{ $.port }} } }
{{- end }}
{{- end }}

{{/*
The four dependencies the NetworkPolicies open (`networkpolicies.yaml`), as a YAML list: each one in
its own namespace on a cluster, or, embedded (ADR 0184), as the chart's pods in the release namespace.
*/}}
{{- define "tranibor.networkDependencies" -}}
{{- $g := .Values.global -}}
- { name: database, port: 5432, namespace: choregos-data, pod: choregos-postgresql, embedded: {{ $g.database.embedded | default false }} }
- { name: temporal, port: 7233, namespace: choregos-temporal, pod: choregos-temporal, embedded: {{ $g.temporal.embedded | default false }} }
- { name: gateway, port: 4000, namespace: choregos-gateway, pod: choregos-litellm, embedded: {{ $g.gateway.embedded | default false }} }
- { name: memory, port: 8432, namespace: choregos-memory, pod: {{ ((index .Values "ecphoria" | default dict).selectorLabels | default dict) | dig "app.kubernetes.io/name" "choregos-ecphoria" }}, embedded: {{ $g.memory.embedded | default false }} }
{{- /* ADR 0208: listed only when embedded, so an install that does not embed them renders what it always did. */}}
{{- if ($g.objectStore).embedded }}
- { name: objectstore, port: 9000, namespace: choregos-objectstore, pod: choregos-objectstore, embedded: true }
{{- end }}
{{- if ($g.oidc).embedded }}
- { name: oidc, port: 8080, namespace: choregos-oidc, pod: choregos-keycloak, embedded: true }
{{- end }}
{{- end -}}

{{/* The NetworkPolicy peer of one of `choregos.networkDependencies`. */}}
{{- define "tranibor.networkDependencyPeer" -}}
{{- if .dependency.embedded -}}
{ podSelector: { matchLabels: { app.kubernetes.io/name: {{ .dependency.pod }} } } }
{{- else -}}
{ namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: {{ .dependency.namespace }} } } }
{{- end -}}
{{- end -}}

{{/*
All in one (ADR 0208): the pieces the chart can embed beyond the data stores, each one switched by its
own `embedded` and each one overridable by the setting that names the site's own.
*/}}
{{- define "tranibor.allInOne" -}}
{{- if ((.Values.global.allInOne | default dict).enabled) -}}true{{- end -}}
{{- end -}}

{{/* An image by tag and, when the digest is known, by digest too: `repo:tag@sha256:...`. */}}
{{- define "tranibor.pinnedImage" -}}
{{ .repository }}:{{ .tag }}{{ with .digest }}@{{ . }}{{ end }}
{{- end -}}

{{- define "tranibor.objectStoreServiceName" -}}
{{- (.Values.global.objectStore).serviceName | default (printf "%s-objectstore" .Release.Name) -}}
{{- end -}}

{{/* The bucket the object store URL names: `s3://bucket/prefix` gives `bucket`. */}}
{{- define "tranibor.objectStoreBucket" -}}
{{- regexReplaceAll "/.*" (trimPrefix "s3://" (.Values.global.objectStore.url | default "")) "" -}}
{{- end -}}

{{/*
The object store's protocol (S87-17): `s3` (default), `azure-blob` or `gcs`, checked against the
URL's scheme and what each one needs. A `memory://` URL (the fake) goes with any kind.
*/}}
{{- define "tranibor.objectStoreKind" -}}
{{- $store := .Values.global.objectStore -}}
{{- $kind := $store.kind | default "s3" -}}
{{- $schemes := dict "s3" "s3://" "azure-blob" "azblob://" "gcs" "gs://" -}}
{{- if not (hasKey $schemes $kind) -}}
{{- fail (printf "global.objectStore.kind=%s unknown: `s3`, `azure-blob` or `gcs`." $kind) -}}
{{- end -}}
{{- $url := $store.url | default "" -}}
{{- $memory := hasPrefix "memory://" $url -}}
{{- if not (or (hasPrefix (get $schemes $kind) $url) $memory) -}}
{{- fail (printf "global.objectStore.kind=%s takes a url that starts with %s, not `%s`." $kind (get $schemes $kind) $url) -}}
{{- end -}}
{{- if and $store.embedded (ne $kind "s3") -}}
{{- fail (printf "global.objectStore.embedded=true deploys an S3 server: it goes with kind `s3`, not `%s`." $kind) -}}
{{- end -}}
{{- if and (eq $kind "azure-blob") (not $store.endpoint) (not $memory) -}}
{{- fail "global.objectStore.kind=azure-blob needs global.objectStore.endpoint, the account's blob endpoint (https://<account>.blob.core.windows.net)." -}}
{{- end -}}
{{- if and (eq $kind "gcs") $store.secretRef -}}
{{- fail "global.objectStore.kind=gcs takes no secretRef: the API uses its pod's GKE Workload Identity. For HMAC keys, use kind `s3` with endpoint https://storage.googleapis.com." -}}
{{- end -}}
{{- $kind -}}
{{- end -}}

{{/* The pod labels the object store's identity needs: Azure Workload Identity injects its token only
     into a pod labelled `azure.workload.identity/use: "true"`. Empty for every other kind. */}}
{{- define "tranibor.objectStorePodLabels" -}}
{{- if and (eq (include "tranibor.objectStoreKind" .) "azure-blob") (not .Values.global.objectStore.secretRef) -}}
azure.workload.identity/use: "true"
{{- end -}}
{{- end -}}

{{/* The endpoint the API, the backup and the bucket Job use: the site's, else the embedded server's. */}}
{{- define "tranibor.objectStoreEndpoint" -}}
{{- if .Values.global.objectStore.endpoint -}}
{{ .Values.global.objectStore.endpoint }}
{{- else if .Values.global.objectStore.embedded -}}
http://{{ include "tranibor.objectStoreServiceName" . }}.{{ .Release.Namespace }}.svc:9000
{{- end -}}
{{- end -}}

{{/* The embedded Keycloak's public host: `tranibor-keycloak.ingress.hostname`, else `auth.<global.domain>`. */}}
{{- define "tranibor.oidcHost" -}}
{{- $kc := index .Values "keycloak" | default dict -}}
{{- (($kc.ingress | default dict).hostname) | default (printf "auth.%s" .Values.global.domain) -}}
{{- end -}}

{{/* The issuer the API and the realm agree on: the site's `global.oidc.issuer`, else the embedded realm's. */}}
{{- define "tranibor.oidcIssuer" -}}
{{- if .Values.global.oidc.issuer -}}
{{ .Values.global.oidc.issuer }}
{{- else if .Values.global.oidc.embedded -}}
{{ ternary "https" "http" (hasPrefix "https://" (.Values.global.publicUrl | default "")) }}://{{ include "tranibor.oidcHost" . }}/realms/{{ ((index .Values "keycloak" | default dict).realm | default dict).name | default "choregos" }}
{{- end -}}
{{- end -}}

{{/*
The labels an adopted piece keeps (#2150): what its pods carry and what its selector and Service match.
`.override` (the piece's `selectorLabels`) replaces `.default` whole when set, because a Deployment's
selector is immutable and an operator's network policies select on it; `choregos.podLabels` adds the
component label and the operator's extra `podLabels` (never in a selector) to it. Empty overrides render
what the chart always rendered.
*/}}
{{- define "tranibor.selectorLabels" -}}
{{- if .override -}}{{- toYaml .override -}}{{- else -}}{{- toYaml .default -}}{{- end -}}
{{- end -}}

{{- define "tranibor.podLabels" -}}
{{- toYaml (merge (dict) .selector (dict "app.kubernetes.io/component" .component) (.extra | default dict)) -}}
{{- end -}}

{{/*
`networkPolicies.dns`, checked and written out (#2277): `{"namespace", "podLabels", "cidrs", "ports"}` as
JSON, the namespace defaulted to `kube-system` when the key is absent, the labels and addresses as
strings, `ports` as given (empty: each policy's own). The chart's policies render their DNS rule from it
(`tranibor.dnsEgress`), and the API and the workers read it as `TRANIBOR_DNS_POLICY` (the ConfigMap
`tranibor-dns-policy`), so that the policies written by code (a plugin server's, a project namespace's)
open the same resolver as the chart's (`tranibor_core.dns_policy`).
*/}}
{{- define "tranibor.dnsSetting" -}}
{{- $dns := .Values.networkPolicies.dns | default dict -}}
{{- $namespace := $dns.namespace | default "" | toString -}}
{{- if not (hasKey $dns "namespace") }}{{ $namespace = "kube-system" }}{{ end -}}
{{- $cidrs := list -}}
{{- range ($dns.cidrs | default list) }}{{ $cidrs = append $cidrs (toString .) }}{{ end -}}
{{- if and (not $namespace) (not $cidrs) -}}
{{- fail "networkPolicies.dns: neither a namespace nor cidrs: no pod could resolve a name. Give the resolver's namespace (kube-system, openshift-dns), its addresses (cidrs), or both." -}}
{{- end -}}
{{- if and $dns.podLabels (not $namespace) -}}
{{- fail "networkPolicies.dns.podLabels selects the resolver's pods in networkPolicies.dns.namespace, which is empty." -}}
{{- end -}}
{{- $labels := dict -}}
{{- range $key, $value := ($dns.podLabels | default dict) }}{{ $_ := set $labels $key (toString $value) }}{{ end -}}
{{- $ports := list -}}
{{- range ($dns.ports | default list) -}}
{{- if not (and .port (has (toString .protocol) (list "UDP" "TCP"))) -}}
{{- fail (printf "networkPolicies.dns.ports: %v needs a port and a protocol (UDP or TCP)." .) -}}
{{- end -}}
{{- $ports = append $ports (dict "port" (int .port) "protocol" (toString .protocol)) -}}
{{- end -}}
{{- toJson (dict "namespace" $namespace "podLabels" $labels "cidrs" $cidrs "ports" $ports) -}}
{{- end -}}

{{/*
`TRANIBOR_DNS_POLICY` for the API and the workers, from the ConfigMap `tranibor-dns-policy` (#2277): the
setting lives in the parent chart, which a subchart's values cannot read. `global.extraEnv` may not set it
(`tranibor.commonEnv`).
*/}}
{{- define "tranibor.dnsPolicyEnv" -}}
# The cluster's resolver (`networkPolicies.dns`) for the policies this process writes (#2277).
- name: TRANIBOR_DNS_POLICY
  valueFrom: { configMapKeyRef: { name: tranibor-dns-policy, key: policy.json } }
{{- end -}}

{{/*
The DNS rule of every egress policy of this chart (S87-16), from `networkPolicies.dns`. A cluster's
resolver is not always `kube-system`:53: OpenShift serves it from `openshift-dns` on 5353 (a policy
is matched after the Service's port is translated), and GKE's NodeLocal DNSCache answers at a
link-local address no namespace holds. `.tcp`: whether this policy's default ports include TCP 53
beside UDP 53, as each policy did before the setting existed; `dns.ports`, when set, replaces both.
The defaults render the rule each policy always rendered, byte for byte. The code builds the same rule
from the same setting (`tranibor_core.dns_policy.network_policy_rule`).
*/}}
{{- define "tranibor.dnsEgress" -}}
{{- $dns := include "tranibor.dnsSetting" .root | fromJson -}}
{{- $namespace := $dns.namespace -}}
{{- $ports := $dns.ports -}}
{{- if not $ports -}}
{{- $ports = list (dict "protocol" "UDP" "port" 53) -}}
{{- if .tcp }}{{ $ports = append $ports (dict "protocol" "TCP" "port" 53) }}{{ end -}}
{{- end -}}
{{- $peers := list -}}
{{- if $namespace -}}
{{- $peer := printf "{ namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: %s } } }" $namespace -}}
{{- with $dns.podLabels -}}
{{- $labels := list -}}
{{- range $key, $value := . }}{{ $labels = append $labels (printf "%s: %s" $key (toString $value | quote)) }}{{ end -}}
{{- $peer = printf "{ namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: %s } }, podSelector: { matchLabels: { %s } } }" $namespace (join ", " $labels) -}}
{{- end -}}
{{- $peers = append $peers $peer -}}
{{- end -}}
{{- range $dns.cidrs }}{{ $peers = append $peers (printf "{ ipBlock: { cidr: %s } }" (toString . | quote)) }}{{ end -}}
{{- $rendered := list -}}
{{- range $ports }}{{ $rendered = append $rendered (printf "{ protocol: %s, port: %d }" (toString .protocol) (int .port)) }}{{ end -}}
- to: [{{ join ", " $peers }}]
  ports: [{{ join ", " $rendered }}]
{{- end -}}

{{/*
Whether the cluster serves the monitoring operator's type `.kind` (`scrape` or `rules`) of the chosen
`monitoring.flavour` (S87-16): `true` or empty. Without the CRD, a ServiceMonitor or a PrometheusRule
fails the whole install (`no matches for kind`), so the chart renders it only when Helm or Argo CD
says the cluster serves it, or when `monitoring.skipCrdCheck` says so for an offline render.
*/}}
{{- define "tranibor.monitoringApi" -}}
{{- $vm := eq (.root.Values.monitoring.flavour | default "prometheus") "victoriametrics" -}}
{{- if eq .kind "scrape" -}}
{{- ternary "operator.victoriametrics.com/v1beta1/VMServiceScrape" "monitoring.coreos.com/v1/ServiceMonitor" $vm -}}
{{- else -}}
{{- ternary "operator.victoriametrics.com/v1beta1/VMRule" "monitoring.coreos.com/v1/PrometheusRule" $vm -}}
{{- end -}}
{{- end -}}

{{- define "tranibor.monitoringServed" -}}
{{- if or .root.Values.monitoring.skipCrdCheck (.root.Capabilities.APIVersions.Has (include "tranibor.monitoringApi" .)) -}}true{{- end -}}
{{- end -}}
