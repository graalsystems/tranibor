{{/*
The components' values keys (ADR 0224 R6). Each subchart reads its values from an internal key, its
directory's name (`api`, `web`, `orchestrator`…), which holds the chart's defaults and nothing else.
An operator sets a component under `tranibor-<component>` (`tranibor-api.runDoor.hostnames`) or, for
the window of ADR 0224, under its former name `choregos-<component>`: neither key carries a default,
so what is under them is what the operator wrote, and a setting written under both is seen.
`templates/values/keys/merged/first.yaml` merges them into the internal key before anything renders.
*/}}

{{/* The components, by their internal key: `tranibor-<key>` is the values key, `choregos-<key>` its former name. */}}
{{- define "tranibor.components" -}}
api web orchestrator tools postgresql temporal litellm ecphoria laya websearch objectstore keycloak
{{- end -}}

{{/*
The dotted paths of the leaves of a map, as a JSON list: (dict "values" <map> "prefix" <path or "">).
A list, a scalar, null and an empty map are leaves: setting one replaces what was there whole.
*/}}
{{- define "tranibor.valueLeaves" -}}
{{- $leaves := list -}}
{{- range $key, $value := .values -}}
{{- $path := ternary $key (printf "%s.%s" $.prefix $key) (empty $.prefix) -}}
{{- if and (kindIs "map" $value) (not (empty $value)) -}}
{{- $leaves = concat $leaves (include "tranibor.valueLeaves" (dict "values" $value "prefix" $path) | fromJsonArray) -}}
{{- else -}}
{{- $leaves = append $leaves $path -}}
{{- end -}}
{{- end -}}
{{- toJson $leaves -}}
{{- end -}}

{{/*
Merges `.from` into `.into` in place, the way Helm merges a values file over the defaults: a map
merges key by key, anything else (a list, a scalar, false, 0, "") replaces, and null removes the key.
Written out rather than sprig's `mergeOverwrite`, whose handling of empty and null values follows the
merge library's version, not Helm's rule.
*/}}
{{- define "tranibor.mergeValues" -}}
{{- $into := .into -}}
{{- range $key, $value := .from -}}
{{- if kindIs "invalid" $value -}}
{{- $_ := unset $into $key -}}
{{- else if and (kindIs "map" $value) (kindIs "map" (get $into $key)) -}}
{{- include "tranibor.mergeValues" (dict "into" (get $into $key) "from" $value) -}}
{{- else -}}
{{- $_ := set $into $key (deepCopy $value) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Reads `tranibor-<component>` and `choregos-<component>` into the component's internal key, once per
render. A path set under both names, or a path under one inside a path set under the other, refuses
to render: which of two values the operator meant is not the chart's to guess. Merging twice gives the
same values.
*/}}
{{- define "tranibor.componentValues" -}}
{{- range $component := splitList " " (include "tranibor.components" $) -}}
{{- $current := printf "tranibor-%s" $component -}}
{{- $former := printf "choregos-%s" $component -}}
{{- $set := dict -}}
{{- range $name := list $current $former -}}
{{- $value := index $.Values $name -}}
{{- if not (kindIs "invalid" $value) -}}
{{- if not (kindIs "map" $value) -}}
{{- fail (printf "%s: a component's values are a map of its settings, not %s" $name (kindOf $value)) -}}
{{- end -}}
{{- $_ := set $set $name $value -}}
{{- end -}}
{{- end -}}
{{- if and (hasKey $set $current) (hasKey $set $former) -}}
{{- $old := include "tranibor.valueLeaves" (dict "values" (get $set $former) "prefix" "") | fromJsonArray -}}
{{- range $path := include "tranibor.valueLeaves" (dict "values" (get $set $current) "prefix" "") | fromJsonArray -}}
{{- range $other := $old -}}
{{- if or (eq $path $other) (hasPrefix (printf "%s." $path) $other) (hasPrefix (printf "%s." $other) $path) -}}
{{- fail (printf "%s.%s and %s.%s are both set: %s is the former name of %s (ADR 0224), read only until the end of its window. Keep the setting under %s and remove it from %s." $current $path $former $other $former $current $current $former) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if not (kindIs "map" (index $.Values $component)) -}}
{{- $_ := set $.Values $component dict -}}
{{- end -}}
{{- range $name := list $former $current -}}
{{- with get $set $name -}}
{{- include "tranibor.mergeValues" (dict "into" (index $.Values $component) "from" .) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
