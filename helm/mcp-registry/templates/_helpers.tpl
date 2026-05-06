{{- define "mcp-registry.name" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mcp-registry.labels" -}}
app.kubernetes.io/name: mcp-registry
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "mcp-registry.selectorLabels" -}}
app.kubernetes.io/name: mcp-registry
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "mcp-registry.postgres.selectorLabels" -}}
app.kubernetes.io/name: mcp-registry-postgres
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "mcp-registry.databaseUrl" -}}
{{- if .Values.postgres.externalUrl -}}
{{ .Values.postgres.externalUrl }}
{{- else -}}
postgres://{{ .Values.postgres.auth.username }}:{{ .Values.postgres.auth.password }}@{{ .Release.Name }}-postgres:5432/{{ .Values.postgres.auth.database }}?sslmode=disable
{{- end }}
{{- end }}
