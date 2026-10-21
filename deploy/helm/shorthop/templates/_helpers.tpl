{{- define "shorthop.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}{{- else -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "shorthop.labels" -}}
app.kubernetes.io/name: shorthop
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "shorthop.selector" -}}
app.kubernetes.io/name: shorthop
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "shorthop.secretName" -}}
{{- .Values.secret.existingSecret | default (include "shorthop.fullname" .) -}}
{{- end -}}
