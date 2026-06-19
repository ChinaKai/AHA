#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)

SERVICE_NAME="aha-src.service"
AHA_HOME="${REPO_ROOT}/.aha"
HOST="127.0.0.1"
PORT="8766"
RUN_ID=""
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
START_SERVICE=1
ENABLE_SERVICE=1
DRY_RUN=0
HEALTH_CHECK=1
HEALTH_TIMEOUT=20
VERSION_VALIDATION=1
AUTH_REQUIRED=1
AUTH_TOKEN_FILE=""
ALLOW_UNSAFE_BIND=0

usage() {
    cat <<'EOF'
Usage: scripts/install_source_user_service.sh [options]

Install a user systemd service for running the source checkout UI. Defaults
to the repository-local .aha home and port 8766.

Options:
  --service-name NAME  User systemd service name (default: aha-src.service)
  --aha-home PATH      AHA data directory passed to --home (default: repo/.aha)
  --host HOST          Dashboard bind host (default: 127.0.0.1)
  --port PORT          Dashboard bind port (default: 8766)
  --run-id RUN_ID      Open a specific run by default
  --python PATH        Python executable (default: python3 on PATH)
  --no-start           Enable the service without starting/restarting it
  --no-enable          Write the service without enabling it
  --no-health-check    Do not poll /api/health after restarting the service
  --health-timeout SEC Seconds to wait for /api/health (default: 20)
  --auth-token-file PATH
                       Require Web UI auth using token from PATH (default: AHA_HOME/web-token)
  --no-auth            Do not require Web UI token auth
  --allow-unsafe-bind  Allow a network-visible bind without token auth
  --skip-version-validation
                       Do not verify the source entrypoint with --version
  --dry-run            Print install plan and service unit without writing files
  -h, --help           Show this help
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

need_arg() {
    local name=$1
    local value=${2-}
    [[ -n "${value}" ]] || die "${name} requires a value"
}

while (($#)); do
    case "$1" in
        --service-name)
            need_arg "$1" "${2-}"
            SERVICE_NAME=$2
            shift 2
            ;;
        --aha-home)
            need_arg "$1" "${2-}"
            AHA_HOME=$2
            shift 2
            ;;
        --host)
            need_arg "$1" "${2-}"
            HOST=$2
            shift 2
            ;;
        --port)
            need_arg "$1" "${2-}"
            PORT=$2
            shift 2
            ;;
        --run-id)
            need_arg "$1" "${2-}"
            RUN_ID=$2
            shift 2
            ;;
        --python)
            need_arg "$1" "${2-}"
            PYTHON_BIN=$2
            shift 2
            ;;
        --no-start)
            START_SERVICE=0
            shift
            ;;
        --no-enable)
            ENABLE_SERVICE=0
            shift
            ;;
        --no-health-check)
            HEALTH_CHECK=0
            shift
            ;;
        --health-timeout)
            need_arg "$1" "${2-}"
            HEALTH_TIMEOUT=$2
            shift 2
            ;;
        --auth-token-file)
            need_arg "$1" "${2-}"
            AUTH_TOKEN_FILE=$2
            AUTH_REQUIRED=1
            shift 2
            ;;
        --no-auth)
            AUTH_REQUIRED=0
            shift
            ;;
        --allow-unsafe-bind)
            ALLOW_UNSAFE_BIND=1
            shift
            ;;
        --skip-version-validation)
            VERSION_VALIDATION=0
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

[[ "${PORT}" =~ ^[0-9]+$ ]] || die "--port must be a number"
if ((PORT < 1 || PORT > 65535)); then
    die "--port must be between 1 and 65535"
fi
[[ "${HEALTH_TIMEOUT}" =~ ^[0-9]+$ ]] || die "--health-timeout must be a number"
if ((HEALTH_TIMEOUT < 1)); then
    die "--health-timeout must be at least 1"
fi

case "${SERVICE_NAME}" in
    *.service) ;;
    *) SERVICE_NAME="${SERVICE_NAME}.service" ;;
esac

PYTHON_BIN=$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "${PYTHON_BIN}")
AHA_HOME=$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "${AHA_HOME}")
if [[ -z "${AUTH_TOKEN_FILE}" ]]; then
    AUTH_TOKEN_FILE="${AHA_HOME}/web-token"
fi
AUTH_TOKEN_FILE=$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "${AUTH_TOKEN_FILE}")
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
SERVICE_PATH="${SYSTEMD_USER_DIR}/${SERVICE_NAME}"

systemd_quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//%/%%}
    printf '"%s"' "${value}"
}

source_pythonpath() {
    if [[ -n "${PYTHONPATH:-}" ]]; then
        printf "%s:%s" "${REPO_ROOT}/src" "${PYTHONPATH}"
    else
        printf "%s" "${REPO_ROOT}/src"
    fi
}

source_version() {
    PYTHONPATH="$(source_pythonpath)" "${PYTHON_BIN}" -m aha_cli --version 2>/dev/null | awk '{print $NF}' || true
}

health_host() {
    local value=$1
    case "${value}" in
        ""|"0.0.0.0")
            printf "127.0.0.1"
            ;;
        "::"|"[::]")
            printf "[::1]"
            ;;
        "::1")
            printf "[::1]"
            ;;
        *:*)
            if [[ "${value}" == \[*\] ]]; then
                printf "%s" "${value}"
            else
                printf "[%s]" "${value}"
            fi
            ;;
        *)
            printf "%s" "${value}"
            ;;
    esac
}

host_is_network_visible() {
    local value=$1
    case "${value}" in
        ""|"0.0.0.0"|"::"|"[::]")
            return 0
            ;;
        "localhost"|"127.0.0.1"|"::1"|"[::1]")
            return 1
            ;;
        127.*)
            return 1
            ;;
        *)
            return 0
            ;;
    esac
}

ensure_auth_token_file() {
    if ((!AUTH_REQUIRED)); then
        return 0
    fi
    mkdir -p "$(dirname -- "${AUTH_TOKEN_FILE}")"
    if [[ ! -s "${AUTH_TOKEN_FILE}" ]]; then
        umask 077
        python3 - <<'PY' > "${AUTH_TOKEN_FILE}"
from __future__ import annotations

import secrets

print(secrets.token_urlsafe(32))
PY
    fi
    chmod 600 "${AUTH_TOKEN_FILE}" >/dev/null 2>&1 || true
    [[ -r "${AUTH_TOKEN_FILE}" && -s "${AUTH_TOKEN_FILE}" ]] || die "auth token file is not readable or is empty: ${AUTH_TOKEN_FILE}"
}

run_health_check() {
    local expected_version=$1
    python3 - "${HEALTH_URL}" "${AHA_HOME}" "${expected_version}" "${HEALTH_TIMEOUT}" <<'PY'
from __future__ import annotations

import json
import sys
import time
import urllib.request

url, expected_home, expected_version, timeout_text = sys.argv[1:5]
deadline = time.time() + float(timeout_text)
last_error = ""
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("ok") is not True:
            raise RuntimeError(f"health payload not ok: {payload!r}")
        if str(payload.get("aha_home") or "") != expected_home:
            raise RuntimeError(f"health AHA home mismatch: {payload.get('aha_home')!r} != {expected_home!r}")
        if expected_version and str(payload.get("aha_version") or "") != expected_version:
            raise RuntimeError(
                f"health version mismatch: {payload.get('aha_version')!r} != {expected_version!r}"
            )
        print(f"Health check ok: {url} home={expected_home} version={payload.get('aha_version') or '-'}")
        raise SystemExit(0)
    except Exception as exc:  # noqa: BLE001
        last_error = str(exc)
        time.sleep(0.5)
print(f"error: health check failed for {url}: {last_error}", file=sys.stderr)
raise SystemExit(1)
PY
}

HEALTH_URL="http://$(health_host "${HOST}"):${PORT}/api/health"

if ((!AUTH_REQUIRED)) && host_is_network_visible "${HOST}" && ((!ALLOW_UNSAFE_BIND)); then
    die "--no-auth with ${HOST}:${PORT} requires --allow-unsafe-bind or --host 127.0.0.1"
fi

exec_start="$(systemd_quote "${PYTHON_BIN}") -m aha_cli --home $(systemd_quote "${AHA_HOME}") ui"
if [[ -n "${RUN_ID}" ]]; then
    exec_start+=" $(systemd_quote "${RUN_ID}")"
fi
exec_start+=" --host $(systemd_quote "${HOST}") --port ${PORT}"
if ((AUTH_REQUIRED)); then
    exec_start+=" --auth-token-file $(systemd_quote "${AUTH_TOKEN_FILE}")"
fi
if ((ALLOW_UNSAFE_BIND)); then
    exec_start+=" --allow-unsafe-bind"
fi

service_unit() {
    cat <<EOF
[Unit]
Description=AHA Source Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_ROOT}
Environment="PYTHONPATH=${REPO_ROOT}/src"
Environment="AHA_HOME=${AHA_HOME}"
Environment="AHA_SOURCE_ROOT=${REPO_ROOT}"
Environment=PYTHONUNBUFFERED=1
ExecStart=${exec_start}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
}

if ((DRY_RUN)); then
    echo "Dry-run: no files written, no services changed"
    echo "Service path: ${SERVICE_PATH}"
    echo "Service: ${SERVICE_NAME}"
    echo "AHA home: ${AHA_HOME}"
    echo "Python: ${PYTHON_BIN}"
    echo "Listening: ${HOST}:${PORT}"
    echo "Enable service: ${ENABLE_SERVICE}"
    echo "Start service: ${START_SERVICE}"
    echo "Health check: ${HEALTH_CHECK}"
    echo "Health URL: ${HEALTH_URL}"
    echo "Version validation: ${VERSION_VALIDATION}"
    echo "Auth required: ${AUTH_REQUIRED}"
    echo "Auth token file: ${AUTH_TOKEN_FILE}"
    echo "Allow unsafe bind: ${ALLOW_UNSAFE_BIND}"
    echo "--- service unit ---"
    service_unit
    exit 0
fi

SOURCE_VERSION=""
if ((VERSION_VALIDATION)); then
    SOURCE_VERSION=$(source_version)
    [[ -n "${SOURCE_VERSION}" ]] || die "source AHA entrypoint did not report a version with --version"
fi

ensure_auth_token_file
mkdir -p "${SYSTEMD_USER_DIR}"

service_unit > "${SERVICE_PATH}"

command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
systemctl --user daemon-reload
if ((ENABLE_SERVICE)); then
    systemctl --user enable "${SERVICE_NAME}"
fi
if ((START_SERVICE)); then
    systemctl --user restart "${SERVICE_NAME}"
fi

HEALTH_VERSION="${SOURCE_VERSION}"
if [[ "${HEALTH_VERSION}" == "unknown" ]]; then
    HEALTH_VERSION=""
fi
if ((HEALTH_CHECK && START_SERVICE)); then
    run_health_check "${HEALTH_VERSION}"
elif ((HEALTH_CHECK)); then
    echo "Health check skipped because --no-start was used"
fi

echo "Installed source systemd user service: ${SERVICE_PATH}"
echo "Service: ${SERVICE_NAME}"
echo "AHA home: ${AHA_HOME}"
if [[ -n "${SOURCE_VERSION}" ]]; then
    echo "Source AHA version: ${SOURCE_VERSION}"
fi
echo "Listening: ${HOST}:${PORT}"
echo "Health: ${HEALTH_URL}"
echo "Status: systemctl --user status ${SERVICE_NAME}"
