#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)

INSTALL_BIN="${HOME}/.local/bin/aha"
SERVICE_NAME="aha.service"
AHA_HOME="${AHA_HOME:-${HOME}/.aha}"
HOST="0.0.0.0"
PORT="8788"
RUN_ID=""
START_SERVICE=1
ENABLE_LINGER=1
CURRENT_USER="${USER:-$(id -un)}"

usage() {
    cat <<'EOF'
Usage: scripts/install_user_service.sh [options]

Build and install AHA as a one-bin executable, then enable a user systemd
service for the dashboard.

Options:
  --bin PATH           Install executable path (default: ~/.local/bin/aha)
  --service-name NAME  User systemd service name (default: aha.service)
  --aha-home PATH      AHA data directory passed to --home (default: ~/.aha)
  --host HOST          Dashboard bind host (default: 0.0.0.0)
  --port PORT          Dashboard bind port (default: 8788)
  --run-id RUN_ID      Open a specific run by default
  --no-start           Enable the service without starting/restarting it
  --no-linger          Do not try to enable user lingering
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

case "${SERVICE_NAME}" in
    *.service) ;;
    *) SERVICE_NAME="${SERVICE_NAME}.service" ;;
esac

INSTALL_BIN=$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "${INSTALL_BIN}")
AHA_HOME=$(python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "${AHA_HOME}")
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
SERVICE_PATH="${SYSTEMD_USER_DIR}/${SERVICE_NAME}"

systemd_quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//%/%%}
    printf '"%s"' "${value}"
}

mkdir -p "$(dirname -- "${INSTALL_BIN}")"
python3 "${REPO_ROOT}/scripts/build_onebin.py" --output "${INSTALL_BIN}"

mkdir -p "${SYSTEMD_USER_DIR}"

exec_start="$(systemd_quote "${INSTALL_BIN}") --home $(systemd_quote "${AHA_HOME}") ui"
if [[ -n "${RUN_ID}" ]]; then
    exec_start+=" $(systemd_quote "${RUN_ID}")"
fi
exec_start+=" --host $(systemd_quote "${HOST}") --port ${PORT}"

cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=AHA Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h
Environment="PATH=${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="AHA_HOME=${AHA_HOME}"
Environment=PYTHONUNBUFFERED=1
ExecStart=${exec_start}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}"

if ((START_SERVICE)); then
    systemctl --user restart "${SERVICE_NAME}"
fi

if ((ENABLE_LINGER)) && command -v loginctl >/dev/null 2>&1; then
    if ! loginctl enable-linger "${CURRENT_USER}" >/dev/null 2>&1; then
        echo "warning: could not enable user lingering; run: sudo loginctl enable-linger ${CURRENT_USER}" >&2
    fi
fi

echo "Installed AHA executable: ${INSTALL_BIN}"
echo "Installed systemd user service: ${SERVICE_PATH}"
echo "Service: ${SERVICE_NAME}"
echo "Listening: ${HOST}:${PORT}"
echo "Status: systemctl --user status ${SERVICE_NAME}"
