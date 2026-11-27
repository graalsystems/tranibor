{{/* The name every object of one relay carries: the release's, with the chart's when it does not hold it. */}}
{{- define "choregos-relay.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 52 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default "choregos-relay" .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 52 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 52 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "choregos-relay.labels" -}}
app.kubernetes.io/name: choregos-relay
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: choregos-relay
{{- end -}}

{{/* What selects the relay's pod, and nothing else of the release. */}}
{{- define "choregos-relay.selectorLabels" -}}
app.kubernetes.io/name: choregos-relay
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: relay
{{- end -}}

{{/* The relay's image: the operator's, by digest when given, by tag otherwise. No release publishes one yet. */}}
{{- define "choregos-relay.image" -}}
{{- $image := .Values.image -}}
{{- if not $image.repository -}}
{{- fail "image.repository is required: no release publishes the relay's image yet. Build docker/relay.Dockerfile, push it to your registry, and name it here (image.registry, image.repository)." -}}
{{- end -}}
{{- $repository := ternary (printf "%s/%s" $image.registry $image.repository) $image.repository (ne ($image.registry | default "") "") -}}
{{- if $image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $image.digest) -}}
{{- fail (printf "image.digest: %q is not a digest (sha256: and 64 hexadecimal characters)" $image.digest) -}}
{{- end -}}
{{ $repository }}@{{ $image.digest }}
{{- else -}}
{{ $repository }}:{{ $image.tag | default .Chart.AppVersion }}
{{- end -}}
{{- end -}}

{{/* Where things are in the pod. The metadata lines' directory and /tmp are the only writable ones. */}}
{{- define "choregos-relay.metadataDir" -}}/var/lib/choregos-relay{{- end -}}
{{- define "choregos-relay.secretDir" -}}/etc/choregos-relay/secret{{- end -}}
{{- define "choregos-relay.ownTlsDir" -}}/etc/choregos-relay/own-tls{{- end -}}

{{/*
What the relay would refuse to start on, refused at render time with the value to change: the relay
says the same when it starts (`tranibor_sni_relay.server.relay_config`), but a pod that loops on it is
found later than a render that fails.
*/}}
{{- define "choregos-relay.validate" -}}
{{- $relay := .Values.relay -}}
{{- $host := "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$" -}}
{{- if not $relay.domain -}}
{{- fail "relay.domain is required: an installation's name is <id>.<relay-domain>" -}}
{{- end -}}
{{- if not (regexMatch $host (toString $relay.domain)) -}}
{{- fail (printf "relay.domain: %q is not a host name in lower case" (toString $relay.domain)) -}}
{{- end -}}
{{- if $relay.hostname -}}
{{- if not (regexMatch $host (toString $relay.hostname)) -}}
{{- fail (printf "relay.hostname: %q is not a host name in lower case" (toString $relay.hostname)) -}}
{{- end -}}
{{- if regexMatch (printf "^[a-z2-7]{12}\\.%s$" (regexQuoteMeta (toString $relay.domain))) (toString $relay.hostname) -}}
{{- fail (printf "relay.hostname: %q is a name an installation's id could take (12 characters under the domain): choose another label" (toString $relay.hostname)) -}}
{{- end -}}
{{- end -}}
{{- if lt (int64 ($relay.retentionDays | default 0)) 1 -}}
{{- fail "relay.retentionDays is required and at least 1: a relay keeps its metadata lines for a fixed retention, which its operator sets (ADR 0230 §7)" -}}
{{- end -}}
{{- range $name, $value := $relay.limits -}}
{{- if lt (int64 $value) 1 -}}
{{- fail (printf "relay.limits.%s: every limit is a positive number" $name) -}}
{{- end -}}
{{- end -}}
{{- if not .Values.existingSecret -}}
{{- fail "existingSecret is required: the name of the Secret you created with the relay's own material (secretKeys). The chart creates none and no value holds a secret." -}}
{{- end -}}
{{- $own := .Values.own -}}
{{- if and $own.ingress.enabled $own.tls.secretName -}}
{{- fail "own.tls.secretName and own.ingress.enabled: one place terminates the own host's TLS, the relay or the Ingress controller; set one of them" -}}
{{- end -}}
{{- if and (ne $own.service.type "ClusterIP") (not $own.tls.secretName) -}}
{{- fail (printf "own.service.type=%s publishes the own host, and nothing would terminate its TLS: agents dial https and wss. Give own.tls.secretName, or keep ClusterIP behind an Ingress (own.ingress)." $own.service.type) -}}
{{- end -}}
{{- include "choregos-relay.passthroughAnnotations" . -}}
{{- end -}}

{{/*
An annotation of the passthrough Service that would put something between the phone and the relay
that reads the stream: a certificate or TLS ports (the load balancer terminates TLS), a TLS or HTTP
protocol (it terminates, or proxies HTTP), a PROXY header (the relay would read it as a ClientHello and
close every connection). Placement annotations pass.
*/}}
{{- define "choregos-relay.passthroughAnnotations" -}}
{{- range $key, $value := (.Values.passthrough.annotations | default dict) -}}
{{- $k := lower $key -}}
{{- $v := lower (toString $value) -}}
{{- if regexMatch "(ssl|cert|https|http2|http3|http-ports|tls-ports|proxy-?protocol|proxy_protocol|proxyprotocol)" $k -}}
{{- fail (printf "passthrough.annotations: %q would terminate TLS or rewrite the stream in front of the relay, which never terminates it (ADR 0230 §4)" $key) -}}
{{- end -}}
{{- if and (regexMatch "protocols?$" $k) (regexMatch "(http|ssl|tls)" $v) -}}
{{- fail (printf "passthrough.annotations: %q=%q would terminate TLS or proxy HTTP in front of the relay, which never terminates it (ADR 0230 §4)" $key (toString $value)) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* The port agents dial the own host on: the operator's, else 443 behind an Ingress, else the Service's. */}}
{{- define "choregos-relay.ownPublicPort" -}}
{{- $own := .Values.own -}}
{{- if $own.publicPort -}}
{{- int $own.publicPort -}}
{{- else if $own.ingress.enabled -}}
443
{{- else -}}
{{- int $own.service.port -}}
{{- end -}}
{{- end -}}

{{/* The own host's name: the operator's, else the domain. */}}
{{- define "choregos-relay.ownHost" -}}
{{- .Values.relay.hostname | default .Values.relay.domain -}}
{{- end -}}
