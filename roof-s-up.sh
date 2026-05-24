#!/usr/bin/env bash
# roof-s-up.sh — Wrapper around docker compose that guarantees the custom-plugin
# mounts from docker-compose.override.yml are loaded.
#
# Why this exists:
# WebODM's official `./webodm.sh start` ends up calling
#   `docker compose -f docker-compose.yml ...`
# As soon as -f is passed explicitly, docker compose stops auto-loading
# docker-compose.override.yml. The Roof-S custom plugins (project_data,
# realign, roof_detect) live ONLY in that override file as bind-mounts —
# so every webodm.sh start silently wipes them from the container.
# Use this wrapper instead.
#
# Subcommands:
#   start|up [services...]   docker compose up -d (with override.yml)
#   stop|down                docker compose down
#   restart [service...]     docker compose restart
#   logs [service]           tail -f docker compose logs
#   plugins                  list plugin files in container + which got registered
#   status|ps                docker compose ps
#
# Optional flags before subcommand:
#   --gpu       include docker-compose.nodeodm.gpu.nvidia.yml
#   --ssl       include docker-compose.ssl.yml
#   --dev       include docker-compose.dev.yml

set -euo pipefail

WEBODM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WEBODM_DIR"

# Build compose -f arguments
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.override.yml)

while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --gpu)  COMPOSE_FILES+=(-f docker-compose.nodeodm.gpu.nvidia.yml) ;;
        --ssl)  COMPOSE_FILES+=(-f docker-compose.ssl.yml) ;;
        --dev)  COMPOSE_FILES+=(-f docker-compose.dev.yml) ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
    shift
done

usage() {
    sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
}

cmd="${1:-}"
shift || true

run_compose() {
    docker compose "${COMPOSE_FILES[@]}" "$@"
}

case "$cmd" in
    start|up)
        run_compose up -d "$@"
        sleep 3
        echo
        echo "=== Registered plugins (from webapp logs) ==="
        docker logs --tail=300 webapp 2>&1 | grep "INFO Registered" | sed 's/^/  /' || true
        ;;
    stop|down)
        run_compose down "$@"
        ;;
    restart)
        run_compose restart "$@"
        sleep 3
        echo
        echo "=== Registered plugins after restart ==="
        docker logs --tail=300 webapp 2>&1 | grep "INFO Registered" | sed 's/^/  /' || true
        ;;
    logs)
        run_compose logs --tail=100 -f "$@"
        ;;
    plugins)
        echo "=== Files in /webodm/coreplugins/ ==="
        docker exec webapp ls /webodm/coreplugins/ | sort | sed 's/^/  /'
        echo
        echo "=== Registered plugins (from logs) ==="
        docker logs --tail=500 webapp 2>&1 | grep "INFO Registered" | sed 's/^/  /' || true
        ;;
    status|ps)
        run_compose ps
        ;;
    -h|--help|"")
        usage
        exit 0
        ;;
    *)
        echo "Unknown command: $cmd"
        echo
        usage
        exit 1
        ;;
esac
