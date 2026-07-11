#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)

INSTALL_BIN="${HOME}/.local/bin/aha"
SERVICE_NAME="aha.service"
RELEASE_REPO="${AHA_RELEASE_REPO:-ChinaKai/AHA}"
RELEASE_VERSION="${AHA_RELEASE_VERSION:-latest}"
RELEASE_ASSET="${AHA_RELEASE_ASSET:-aha}"
DOWNLOAD_URL="${AHA_RELEASE_URL:-}"
ARTIFACT_PATH=""
BUILD_FROM_SOURCE=0
AHA_HOME="${HOME}/.aha"
HOST="127.0.0.1"
PORT="8788"
RUN_ID=""
START_SERVICE=1
ENABLE_LINGER=1
DRY_RUN=0
HEALTH_CHECK=1
HEALTH_TIMEOUT=20
UPGRADE_VALIDATION=1
AUTH_REQUIRED=1
AUTH_TOKEN_FILE=""
ALLOW_UNSAFE_BIND=0
CURRENT_USER="${USER:-$(id -un)}"

usage() {
    cat <<'EOF'
Usage: scripts/install_user_service.sh [options]

Install AHA as a one-bin executable from a release artifact, then enable a
user systemd service for the dashboard.

By default the installer downloads the latest GitHub Release asset from
ChinaKai/AHA. Use --artifact to install a local prebuilt onebin, --download-url
for a direct release URL, or --build-from-source for the legacy source build.

Options:
  --bin PATH           Install executable path (default: ~/.local/bin/aha)
  --service-name NAME  User systemd service name (default: aha.service)
  --repo OWNER/REPO    GitHub release repository (default: ChinaKai/AHA)
  --version VERSION    Release tag or "latest" (default: latest)
  --asset-name NAME    Release asset name (default: aha)
  --download-url URL   Download executable from an explicit URL
  --artifact PATH      Install an existing local onebin artifact
  --build-from-source  Build onebin from this source checkout before installing
  --aha-home PATH      AHA data directory passed to --home (default: ~/.aha)
  --host HOST          Dashboard bind host (default: 127.0.0.1)
  --port PORT          Dashboard bind port (default: 8788)
  --run-id RUN_ID      Open a specific run by default
  --no-start           Enable the service without starting/restarting it
  --no-linger          Do not try to enable user lingering
  --no-health-check    Do not poll /api/health after restarting the service
  --health-timeout SEC Seconds to wait for /api/health (default: 20)
  --auth-token-file PATH
                       Require Web UI auth using token from PATH (default: AHA_HOME/web-token)
  --no-auth            Do not require Web UI token auth
  --allow-unsafe-bind  Allow a network-visible bind without token auth
  --skip-upgrade-validation
                       Do not verify the installed executable with --version
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
        --bin)
            need_arg "$1" "${2-}"
            INSTALL_BIN=$2
            shift 2
            ;;
        --service-name)
            need_arg "$1" "${2-}"
            SERVICE_NAME=$2
            shift 2
            ;;
        --repo)
            need_arg "$1" "${2-}"
            RELEASE_REPO=$2
            shift 2
            ;;
        --version)
            need_arg "$1" "${2-}"
            RELEASE_VERSION=$2
            shift 2
            ;;
        --asset-name)
            need_arg "$1" "${2-}"
            RELEASE_ASSET=$2
            shift 2
            ;;
        --download-url)
            need_arg "$1" "${2-}"
            DOWNLOAD_URL=$2
            shift 2
            ;;
        --artifact)
            need_arg "$1" "${2-}"
            ARTIFACT_PATH=$2
            shift 2
            ;;
        --build-from-source)
            BUILD_FROM_SOURCE=1
            shift
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
        --no-start)
            START_SERVICE=0
            shift
            ;;
        --no-linger)
            ENABLE_LINGER=0
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
        --skip-upgrade-validation)
            UPGRADE_VALIDATION=0
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

INSTALL_BIN=$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "${INSTALL_BIN}")
AHA_HOME=$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "${AHA_HOME}")
if [[ -z "${AUTH_TOKEN_FILE}" ]]; then
    AUTH_TOKEN_FILE="${AHA_HOME}/web-token"
fi
AUTH_TOKEN_FILE=$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "${AUTH_TOKEN_FILE}")
if [[ -n "${ARTIFACT_PATH}" ]]; then
    ARTIFACT_PATH=$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "${ARTIFACT_PATH}")
fi
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
SERVICE_PATH="${SYSTEMD_USER_DIR}/${SERVICE_NAME}"

SOURCE_COUNT=0
[[ -n "${ARTIFACT_PATH}" ]] && SOURCE_COUNT=$((SOURCE_COUNT + 1))
[[ -n "${DOWNLOAD_URL}" ]] && SOURCE_COUNT=$((SOURCE_COUNT + 1))
((BUILD_FROM_SOURCE)) && SOURCE_COUNT=$((SOURCE_COUNT + 1))
if ((SOURCE_COUNT > 1)); then
    die "choose only one of --artifact, --download-url, or --build-from-source"
fi

INSTALL_SOURCE="release-download"
if [[ -n "${ARTIFACT_PATH}" ]]; then
    INSTALL_SOURCE="artifact"
elif [[ -n "${DOWNLOAD_URL}" ]]; then
    INSTALL_SOURCE="download-url"
elif ((BUILD_FROM_SOURCE)); then
    INSTALL_SOURCE="build-from-source"
fi

systemd_quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//%/%%}
    printf '"%s"' "${value}"
}

systemd_env_line() {
    local key=$1
    local value=$2
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//%/%%}
    printf 'Environment="%s=%s"\n' "${key}" "${value}"
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

binary_version() {
    local bin=$1
    [[ -x "${bin}" ]] || return 0
    "${bin}" --version 2>/dev/null | awk '{print $NF}' || true
}

release_asset_url() {
    python3 - "${RELEASE_REPO}" "${RELEASE_VERSION}" "${RELEASE_ASSET}" <<'PY'
from __future__ import annotations

import sys
from urllib.parse import quote

repo, version, asset = sys.argv[1:4]
asset_path = quote(asset, safe="")
if version == "latest":
    print(f"https://github.com/{repo}/releases/latest/download/{asset_path}")
else:
    print(f"https://github.com/{repo}/releases/download/{quote(version, safe='')}/{asset_path}")
PY
}

download_artifact() {
    local url=$1
    local output=$2
    python3 - "${url}" "${output}" <<'PY'
from __future__ import annotations

from pathlib import Path
import shutil
import sys
from urllib.request import Request, urlopen

url, output = sys.argv[1:3]
target = Path(output)
request = Request(url, headers={"User-Agent": "aha-installer"})
with urlopen(request, timeout=120) as response:
    with target.open("wb") as handle:
        shutil.copyfileobj(response, handle)
PY
}

install_release_artifact() {
    local staged=$1
    local installed_version=""

    chmod 0755 "${staged}"
    if ((UPGRADE_VALIDATION)); then
        installed_version=$(binary_version "${staged}")
        [[ -n "${installed_version}" ]] || die "downloaded AHA executable did not report a version with --version"
    fi

    mkdir -p "$(dirname -- "${INSTALL_BIN}")"
    install -m 0755 "${staged}" "${INSTALL_BIN}"
    printf "%s" "${installed_version}"
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

exec_start="$(systemd_quote "${INSTALL_BIN}") --home $(systemd_quote "${AHA_HOME}") ui"
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
Description=AHA Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h
EOF
    systemd_env_line "PATH" "${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    systemd_env_line "AHA_HOME" "${AHA_HOME}"
    systemd_env_line "AHA_INSTALL_BIN" "${INSTALL_BIN}"
    systemd_env_line "AHA_SERVICE_NAME" "${SERVICE_NAME}"
    systemd_env_line "AHA_RELEASE_REPO" "${RELEASE_REPO}"
    systemd_env_line "AHA_RELEASE_VERSION" "${RELEASE_VERSION}"
    systemd_env_line "AHA_RELEASE_ASSET" "${RELEASE_ASSET}"
    systemd_env_line "AHA_RELEASE_URL" "${DOWNLOAD_URL}"
    systemd_env_line "PYTHONUNBUFFERED" "1"
    cat <<EOF
ExecStart=${exec_start}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
}

RESOLVED_DOWNLOAD_URL=""
if [[ "${INSTALL_SOURCE}" == "release-download" ]]; then
    RESOLVED_DOWNLOAD_URL=$(release_asset_url)
elif [[ "${INSTALL_SOURCE}" == "download-url" ]]; then
    RESOLVED_DOWNLOAD_URL="${DOWNLOAD_URL}"
fi

if ((DRY_RUN)); then
    echo "Dry-run: no files written, no executable downloaded or built, no services changed"
    echo "Install executable: ${INSTALL_BIN}"
    echo "Install source: ${INSTALL_SOURCE}"
    echo "Release repo: ${RELEASE_REPO}"
    echo "Release version: ${RELEASE_VERSION}"
    echo "Release asset: ${RELEASE_ASSET}"
    if [[ -n "${RESOLVED_DOWNLOAD_URL}" ]]; then
        echo "Download URL: ${RESOLVED_DOWNLOAD_URL}"
    fi
    if [[ -n "${ARTIFACT_PATH}" ]]; then
        echo "Artifact path: ${ARTIFACT_PATH}"
    fi
    echo "Service path: ${SERVICE_PATH}"
    echo "Service: ${SERVICE_NAME}"
    echo "AHA home: ${AHA_HOME}"
    echo "Listening: ${HOST}:${PORT}"
    echo "Start service: ${START_SERVICE}"
    echo "Enable linger: ${ENABLE_LINGER}"
    echo "Health check: ${HEALTH_CHECK}"
    echo "Health URL: ${HEALTH_URL}"
    echo "Upgrade validation: ${UPGRADE_VALIDATION}"
    echo "Auth required: ${AUTH_REQUIRED}"
    echo "Auth token file: ${AUTH_TOKEN_FILE}"
    echo "Allow unsafe bind: ${ALLOW_UNSAFE_BIND}"
    echo "--- service unit ---"
    service_unit
    exit 0
fi

PREVIOUS_VERSION=""
if ((UPGRADE_VALIDATION)) && [[ -x "${INSTALL_BIN}" ]]; then
    PREVIOUS_VERSION=$(binary_version "${INSTALL_BIN}")
fi

INSTALLED_VERSION=""
if [[ "${INSTALL_SOURCE}" == "build-from-source" ]]; then
    mkdir -p "$(dirname -- "${INSTALL_BIN}")"
    python3 "${REPO_ROOT}/scripts/build_onebin.py" --output "${INSTALL_BIN}"
    if ((UPGRADE_VALIDATION)); then
        INSTALLED_VERSION=$(binary_version "${INSTALL_BIN}")
        [[ -n "${INSTALLED_VERSION}" ]] || die "installed AHA executable did not report a version with --version"
    fi
else
    TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/aha-install.XXXXXX")
    trap 'rm -rf "${TMP_DIR}"' EXIT
    STAGED_BIN="${TMP_DIR}/aha"
    if [[ "${INSTALL_SOURCE}" == "artifact" ]]; then
        [[ -f "${ARTIFACT_PATH}" ]] || die "artifact does not exist: ${ARTIFACT_PATH}"
        cp "${ARTIFACT_PATH}" "${STAGED_BIN}"
    else
        download_artifact "${RESOLVED_DOWNLOAD_URL}" "${STAGED_BIN}"
    fi
    INSTALLED_VERSION=$(install_release_artifact "${STAGED_BIN}")
fi

ensure_auth_token_file
mkdir -p "${SYSTEMD_USER_DIR}"

service_unit > "${SERVICE_PATH}"

command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}"

if ((START_SERVICE)); then
    systemctl --user restart "${SERVICE_NAME}"
fi

if ((HEALTH_CHECK && START_SERVICE)); then
    run_health_check "${INSTALLED_VERSION}"
elif ((HEALTH_CHECK)); then
    echo "Health check skipped because --no-start was used"
fi

if ((ENABLE_LINGER)) && command -v loginctl >/dev/null 2>&1; then
    if ! loginctl enable-linger "${CURRENT_USER}" >/dev/null 2>&1; then
        echo "warning: could not enable user lingering; run: sudo loginctl enable-linger ${CURRENT_USER}" >&2
    fi
fi

echo "Installed AHA executable: ${INSTALL_BIN}"
if [[ -n "${PREVIOUS_VERSION}" ]]; then
    echo "Previous AHA version: ${PREVIOUS_VERSION}"
fi
if [[ -n "${INSTALLED_VERSION}" ]]; then
    echo "Installed AHA version: ${INSTALLED_VERSION}"
fi
echo "Installed systemd user service: ${SERVICE_PATH}"
echo "Service: ${SERVICE_NAME}"
echo "Listening: ${HOST}:${PORT}"
echo "Health: ${HEALTH_URL}"
echo "Status: systemctl --user status ${SERVICE_NAME}"
