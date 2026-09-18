#!/usr/bin/env bash
# Does the chart refuse what it must refuse?
#
# The Helm chart's main value is not the manifests -- it is the combinations it
# will NOT render. Each refusal below encodes a failure that is either silent at
# runtime (two writers on one SQLite file) or presents as a different problem
# entirely (a pod OOM-killed against a limit it never saw, because with no limit
# set blurd sizes itself from the NODE's memory).
#
# A guardrail that stops firing is worse than no guardrail: it is a promise the
# chart has quietly stopped keeping. So each one is asserted here, and each
# positive case is asserted to still render.
#
# Run:  bash tests/helm_guardrails.sh
# Needs: helm. kubeconform is optional and adds API schema validation.
set -uo pipefail

CHART="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/helm/blurd"
PASS=0; FAIL=0
OK="--set dashboard.password=x"          # the minimum a render needs

refuses() {
  local desc="$1"; shift
  local err
  if err=$(helm template t "$CHART" "$@" 2>&1 >/dev/null); then
    printf "  FAIL  %-46s rendered, but must be refused\n" "$desc"; FAIL=$((FAIL+1))
  elif ! grep -q "blurd:" <<<"$err"; then
    # Refused, but by a template error rather than by a guardrail -- that is a
    # broken chart wearing a passing test's clothes.
    printf "  FAIL  %-46s refused without a blurd message\n" "$desc"; FAIL=$((FAIL+1))
  else
    printf "  PASS  %-46s %s\n" "$desc" "$(grep -oE 'blurd: [^\\]*' <<<"$err" | head -1)"
    PASS=$((PASS+1))
  fi
}

renders() {
  local desc="$1"; shift
  if helm template t "$CHART" "$@" >/dev/null 2>&1; then
    printf "  PASS  %-46s\n" "$desc"; PASS=$((PASS+1))
  else
    printf "  FAIL  %-46s refused, but is a valid configuration\n" "$desc"
    helm template t "$CHART" "$@" 2>&1 >/dev/null | grep -oE 'blurd: [^\\]*' | head -1 | sed 's/^/          /'
    FAIL=$((FAIL+1))
  fi
}

command -v helm >/dev/null || { echo "helm not on PATH"; exit 2; }
echo "chart: $CHART"
echo
echo "must be refused"
refuses "sqlite + 3 replicas"          $OK --set replicaCount=3
refuses "sqlite + autoscaling"         $OK --set autoscaling.enabled=true
refuses "local blobs + 2 replicas"     $OK --set replicaCount=2 --set db.backend=postgres --set db.dsn=p
refuses "postgres without a DSN"       $OK --set db.backend=postgres
refuses "mongo without a DSN"          $OK --set db.backend=mongo
refuses "unknown db backend"           $OK --set db.backend=mysql
refuses "unknown storage backend"      $OK --set storage.backend=gluster
refuses "s3 without an endpoint"       $OK --set storage.backend=s3
refuses "s3 without a bucket"          $OK --set storage.backend=s3 --set storage.s3.endpoint=http://m:9000 --set storage.s3.bucket=""
refuses "no memory limit"              $OK --set resources.limits.memory=null
refuses "no cpu limit"                 $OK --set resources.limits.cpu=null
refuses "grace period == drain"        $OK --set terminationGracePeriodSeconds=20
refuses "grace period < drain"         $OK --set terminationGracePeriodSeconds=10
refuses "dashboard without a password" --set nothing=here
refuses "no source of models"          $OK --set models.initContainer.enabled=false
refuses "RWO volume + 3 replicas"      $OK --set persistence.enabled=true --set replicaCount=3 \
                                           --set db.backend=postgres --set db.dsn=p --set storage.backend=s3 \
                                           --set storage.s3.endpoint=http://m:9000
refuses "local blobs, replicas, no PVC" $OK --set replicaCount=3 --set db.backend=postgres --set db.dsn=p
refuses "local blobs, replicas, RWO PVC" $OK --set replicaCount=3 --set db.backend=postgres --set db.dsn=p \
                                           --set persistence.enabled=true

echo
echo "must render"
renders "default single replica"       $OK
renders "sqlite + a PVC"               $OK --set persistence.enabled=true
renders "postgres + s3, 3 replicas"    $OK --set replicaCount=3 --set db.backend=postgres --set db.existingSecret=s \
                                           --set storage.backend=s3 --set storage.s3.endpoint=http://m:9000
renders "mongo + s3, 3 replicas"       $OK --set replicaCount=3 --set db.backend=mongo --set db.existingSecret=s \
                                           --set storage.backend=s3 --set storage.s3.endpoint=http://m:9000
renders "autoscaling on postgres"      $OK --set db.backend=postgres --set db.existingSecret=s \
                                           --set storage.backend=s3 --set storage.s3.endpoint=http://m:9000 \
                                           --set autoscaling.enabled=true
renders "ingress + tls + pdb"          $OK --set ingress.enabled=true --set podDisruptionBudget.enabled=true \
                                           --set ingress.tls[0].secretName=tls
renders "dashboard off, no password"   --set dashboard.enabled=false
renders "pre-populated models claim"   $OK --set models.initContainer.enabled=false --set models.existingClaim=m
# Distributed WITHOUT object storage: proven to pass conformance on 3 replicas
# sharing one volume, so the chart must allow it.
renders "local blobs on an RWX volume"  $OK --set replicaCount=3 --set db.backend=postgres --set db.existingSecret=s \
                                           --set persistence.enabled=true --set persistence.accessMode=ReadWriteMany
renders "local blobs, shared existingClaim" $OK --set replicaCount=3 --set db.backend=postgres --set db.existingSecret=s \
                                           --set persistence.enabled=true --set persistence.existingClaim=nfs \
                                           --set persistence.shared=true

echo
echo "rendered manifests"
if command -v kubeconform >/dev/null; then
  for cfg in "$OK" "$OK --set replicaCount=3 --set db.backend=postgres --set db.existingSecret=s --set storage.backend=s3 --set storage.s3.endpoint=http://m:9000"; do
    out=$(helm template t "$CHART" $cfg 2>/dev/null | kubeconform -strict -summary -kubernetes-version 1.29.0 - 2>&1 | tail -1)
    if grep -q "Invalid: 0, Errors: 0" <<<"$out" && ! grep -q "0 resource found" <<<"$out"; then
      printf "  PASS  %-46s %s\n" "schema valid" "$out"; PASS=$((PASS+1))
    else
      printf "  FAIL  %-46s %s\n" "schema invalid" "$out"; FAIL=$((FAIL+1))
    fi
  done
else
  echo "  SKIP  kubeconform not installed (API schema validation)"
fi

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
