{{- define "tranibor-temporal.name" -}}
{{- include "tranibor.temporalServiceName" . -}}
{{- end -}}
{{- define "tranibor-temporal.serverSecret" -}}
{{- .Values.tls.serverSecretName | default (printf "%s-tls" (include "tranibor-temporal.name" .)) -}}
{{- end -}}
{{- define "tranibor-temporal.clientSecret" -}}
{{- .Values.tls.clientSecretName | default (printf "%s-client-tls" (include "tranibor-temporal.name" .)) -}}
{{- end -}}
{{- define "tranibor-temporal.claim" -}}
{{- include "tranibor.temporalClaim" (dict "root" . "persistence" .Values.persistence) -}}
{{- end -}}
