{{/*
A `modelInfo` value as flow YAML that keeps its numbers numbers (#2150). Helm reads every number as a float64
and `toYaml` writes a small one as `1e-08`, which YAML 1.1 (PyYAML, what LiteLLM reads the file with) takes
for a STRING: a price came out as `'1e-08'` and only LiteLLM's own cast saved the cost. A whole number is
written as an integer, any other one as a plain decimal (`0.00000001`).
*/}}
{{- define "choregos.litellm.value" -}}
{{- if kindIs "map" . -}}
{ {{- $first := true -}}{{- range $k, $v := . -}}{{- if not $first -}}, {{ end -}}{{- $first = false -}}{{ $k | toJson }}: {{ include "choregos.litellm.value" $v }}{{- end -}} }
{{- else if kindIs "slice" . -}}
[ {{- $first := true -}}{{- range . -}}{{- if not $first -}}, {{ end -}}{{- $first = false -}}{{ include "choregos.litellm.value" . }}{{- end -}} ]
{{- else if kindIs "float64" . -}}
{{- if eq . (floor .) -}}{{ int64 . }}{{- else -}}{{ regexReplaceAll "0+$" (printf "%.15f" .) "" }}{{- end -}}
{{- else -}}
{{ toJson . }}
{{- end -}}
{{- end -}}
