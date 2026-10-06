{{/*
Names and labels
*/}}
{{- define "blurd.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "blurd.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "blurd.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "blurd.labels" -}}
helm.sh/chart: {{ include "blurd.chart" . }}
{{ include "blurd.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: redaction
{{- end }}

{{- define "blurd.selectorLabels" -}}
app.kubernetes.io/name: {{ include "blurd.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "blurd.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "blurd.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "blurd.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{/*
Does this release need a Secret of its own? Only when a value was given inline
rather than pointed at an existing Secret.
*/}}
{{- define "blurd.createSecret" -}}
{{- $need := false -}}
{{- if and .Values.dashboard.enabled .Values.dashboard.password (not .Values.dashboard.existingSecret) }}{{- $need = true -}}{{- end }}
{{- if and (ne .Values.db.backend "sqlite") .Values.db.dsn (not .Values.db.existingSecret) }}{{- $need = true -}}{{- end }}
{{- if and (eq .Values.storage.backend "s3") .Values.storage.s3.accessKey (not .Values.storage.s3.existingSecret) }}{{- $need = true -}}{{- end }}
{{- $need -}}
{{- end }}


{{/*
=============================================================================
Guardrails.

Every one of these encodes a failure that has actually happened, or that the
code refuses at runtime. Failing at `helm install` is a much better place to
find out than at 3am, so these are hard errors, not warnings.
=============================================================================
*/}}
{{- define "blurd.validate" -}}

{{- /* SQLite is a single-writer file. Two writers is silent corruption -- blurd
       itself refuses to start a second instance against one home, so this would
       fail at rollout anyway, one pod at a time, looking like a crash loop. */ -}}
{{- if and (eq .Values.db.backend "sqlite") (gt (int .Values.replicaCount) 1) }}
{{- fail "\n\nblurd: db.backend=sqlite cannot run more than one replica.\n\nSQLite is a single-writer file; two replicas sharing one home corrupt it\nsilently, and blurd refuses to start the second instance.\n\nEither set replicaCount=1, or move metadata to a shared store:\n  --set db.backend=postgres --set db.existingSecret=blurd-db\n  --set db.backend=mongo    --set db.existingSecret=blurd-db\n" }}
{{- end }}

{{- if and (eq .Values.db.backend "sqlite") .Values.autoscaling.enabled }}
{{- fail "\n\nblurd: autoscaling cannot be enabled with db.backend=sqlite.\n\nThe HPA would scale to a second replica, which SQLite cannot survive.\nMove metadata to postgres or mongo first.\n" }}
{{- end }}

{{- /* Local blobs across replicas are fine IF the volume is genuinely shared.
       Measured both ways: with a volume per replica, the metadata read returns
       200 (it is in Postgres) while the blob returns 404 on whichever replica
       did not process the image -- intermittent failures on a fraction of
       reads, which is far worse to diagnose than an outage. With ONE shared
       volume, three replicas behind a load balancer pass all 113 conformance
       checks.

       So this refuses the unshared case only, and makes the operator say so:
       the chart cannot inspect an existingClaim's access mode, and defaulting
       to "assume it is shared" would turn a silent data bug into a silent data
       bug with extra steps. */ -}}
{{- if and (eq .Values.storage.backend "local") (gt (int .Values.replicaCount) 1) }}
{{- $shared := and .Values.persistence.enabled (or .Values.persistence.shared (eq .Values.persistence.accessMode "ReadWriteMany")) }}
{{- if not $shared }}
{{- fail "\n\nblurd: storage.backend=local with several replicas needs a SHARED volume.\n\nWith a volume per replica, blobs written by one pod are invisible to the\nothers: the metadata read succeeds (it is in the shared database) while the\nblob 404s on whichever replica did not process the image. Intermittent\nfailures on a fraction of reads, not an outage -- much harder to diagnose.\n\nTwo valid answers:\n\n  object storage (recommended -- nothing to share):\n    --set storage.backend=s3 --set storage.s3.endpoint=...\n\n  a ReadWriteMany volume (NFS, CephFS, EFS, Azure Files):\n    --set persistence.enabled=true --set persistence.accessMode=ReadWriteMany\n    # or, for an existingClaim this chart cannot inspect:\n    --set persistence.enabled=true --set persistence.shared=true\n" }}
{{- end }}
{{- end }}

{{- if not (has .Values.db.backend (list "sqlite" "postgres" "mongo")) }}
{{- fail (printf "\n\nblurd: unknown db.backend %q. Known: sqlite, postgres, mongo.\n" .Values.db.backend) }}
{{- end }}

{{- if and (ne .Values.db.backend "sqlite") (not .Values.db.dsn) (not .Values.db.existingSecret) }}
{{- fail (printf "\n\nblurd: db.backend=%s needs a connection string.\n\nSet db.existingSecret (preferred -- a DSN carries a password) or db.dsn.\n" .Values.db.backend) }}
{{- end }}

{{- if not (has .Values.storage.backend (list "local" "s3")) }}
{{- fail (printf "\n\nblurd: unknown storage.backend %q. Known: local, s3.\n" .Values.storage.backend) }}
{{- end }}

{{- if eq .Values.storage.backend "s3" }}
{{- if or (not .Values.storage.s3.endpoint) (not .Values.storage.s3.bucket) }}
{{- fail "\n\nblurd: storage.backend=s3 needs storage.s3.endpoint and storage.s3.bucket.\n" }}
{{- end }}
{{- end }}

{{- /* The sizing model reads the cgroup. With no memory limit the cgroup says
       "max" and blurd falls back to the NODE's memory -- so a pod on a big node
       starts far more workers than it can feed and is OOM-killed against a
       limit it never saw. This is the single most damaging misconfiguration
       available, so the chart will not render without a limit. */ -}}
{{- if not (dig "limits" "memory" "" .Values.resources) }}
{{- fail "\n\nblurd: resources.limits.memory is required.\n\nblurd sizes its worker pool from the cgroup. With no memory limit the cgroup\nreports \"max\", blurd falls back to the NODE's memory, and starts as many\nworkers as the node could feed -- then gets OOM-killed against a limit it\nnever saw.\n\nMeasured: peak ~= 120Mi + 90Mi per worker (+ queue budget).\n  512Mi -> 3 workers    1Gi -> 7 workers\n" }}
{{- end }}

{{- if not (dig "limits" "cpu" "" .Values.resources) }}
{{- fail "\n\nblurd: resources.limits.cpu is required.\n\nWithout a CPU limit the cgroup reports the node's cores, and blurd sizes its\nworker pool against cores it will never get.\n" }}
{{- end }}

{{- /* A pod killed before it drains loses its queued uploads: they live only in
       that replica's memory, because blurd never spools source bytes to disk. */ -}}
{{- if le (int .Values.terminationGracePeriodSeconds) (int .Values.drainSeconds) }}
{{- fail (printf "\n\nblurd: terminationGracePeriodSeconds (%v) must exceed drainSeconds (%v).\n\nOn SIGTERM blurd reports 503 so the readiness probe pulls it out of the\nService, then finishes what it started. If the kubelet SIGKILLs it first, the\nqueued uploads in that pod's memory are lost and their producers are told to\nresubmit.\n" .Values.terminationGracePeriodSeconds .Values.drainSeconds) }}
{{- end }}

{{- if and .Values.dashboard.enabled (not .Values.dashboard.password) (not .Values.dashboard.existingSecret) }}
{{- fail "\n\nblurd: dashboard.enabled=true needs a password.\n\nSet dashboard.existingSecret (preferred) or dashboard.password, or set\ndashboard.enabled=false. Without one the dashboard is inert anyway.\n" }}
{{- end }}

{{- /* An RWO volume cannot be mounted by pods on different nodes, and the
       rollout will hang on the new pod waiting for a volume the old one holds. */ -}}
{{- if and .Values.persistence.enabled (gt (int .Values.replicaCount) 1) (eq .Values.persistence.accessMode "ReadWriteOnce") (not .Values.persistence.shared) }}
{{- fail "\n\nblurd: persistence with ReadWriteOnce cannot back more than one replica.\n\nA production deployment needs no PVC at all: metadata goes to postgres/mongo\nand blobs to object storage.\n" }}
{{- end }}

{{- /* Two ways to get models, and a pod with neither cannot process anything:
       it starts, answers health checks, and fails every submission. */ -}}
{{- if and (not .Values.models.initContainer.enabled) (not .Values.models.existingClaim) }}
{{- fail "\n\nblurd: no source of detector models.\n\nEnable models.initContainer.enabled, or pre-populate a volume and set\nmodels.existingClaim. A pod without models starts healthy and then fails\nEVERY submission, which is the worst way to learn about it.\n" }}
{{- end }}

{{- end }}
