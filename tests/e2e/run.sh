#!/bin/sh
set -eu

project="cc-e2e-$(date +%s)-$$"
compose_file="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/compose.yml"
CC_E2E_CTFD_IMAGE="${project}-ctfd:3.8.7"
export CC_E2E_CTFD_IMAGE

case "$project" in
  cc-e2e-*) ;;
  *) echo "refusing unsafe compose project name" >&2; exit 1 ;;
esac

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [ "$status" -ne 0 ]; then
    docker compose -p "$project" -f "$compose_file" ps -a || true
    docker compose -p "$project" -f "$compose_file" logs --no-color --tail=250 plugin-prep ctfd docker-engine db || true
  fi
  docker compose -p "$project" -f "$compose_file" down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker image rm "$CC_E2E_CTFD_IMAGE" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT INT TERM

docker compose -p "$project" -f "$compose_file" up -d --wait
dind_id="$(docker compose -p "$project" -f "$compose_file" ps -q docker-engine)"
test -n "$dind_id"

docker save python:3.12-alpine | docker exec -i "$dind_id" docker load >/dev/null
docker exec "$dind_id" docker tag python:3.12-alpine local.test/ctf/python:3.12-alpine
docker exec "$dind_id" docker volume create \
  --label org.ctfd.challenge-containers.volume-policy=cc-e2e \
  --label org.ctfd.challenge-containers.volume-policy-revision=1 \
  --label org.ctfd.challenge-containers.logical-volume=assets \
  cc-e2e-assets >/dev/null

docker compose -p "$project" -f "$compose_file" exec -T ctfd \
  python /opt/CTFd/CTFd/plugins/challenge-containers/tests/e2e/scenario.py bootstrap

docker compose -p "$project" -f "$compose_file" restart ctfd
docker compose -p "$project" -f "$compose_file" up -d --wait ctfd

docker compose -p "$project" -f "$compose_file" exec -T ctfd \
  python /opt/CTFd/CTFd/plugins/challenge-containers/tests/e2e/scenario.py lifecycle

docker compose -p "$project" -f "$compose_file" exec -T ctfd \
  python /opt/CTFd/CTFd/plugins/challenge-containers/tests/e2e/scenario.py verify-expiry

docker compose -p "$project" -f "$compose_file" exec -T ctfd \
  python /opt/CTFd/CTFd/plugins/challenge-containers/tests/e2e/scenario.py disable-freshness

docker compose -p "$project" -f "$compose_file" restart ctfd
docker compose -p "$project" -f "$compose_file" up -d --wait ctfd
docker compose -p "$project" -f "$compose_file" exec -T ctfd \
  python /opt/CTFd/CTFd/plugins/challenge-containers/tests/e2e/scenario.py verify-freshness-disabled

echo "challenge-containers isolated E2E passed ($project)"
