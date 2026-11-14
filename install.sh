#!/bin/bash
# BatoWatch installer for Batocera: who connects to this box, and what its
# hardware is doing, with history, alerts and a dashboard. Run it again to
# update; batowatch.conf, aliases.conf and the history are kept.
#
#   curl -L https://raw.githubusercontent.com/jimmyayoade/batowatch/main/install.sh | bash
#
# It installs a tagged release: the newest one, or the one named:
#   curl -L <same url> | BATOWATCH_VERSION=1.1.0 bash
# BUA's installer runs this script with the version BUA was last updated to.

BATOWATCH_REPO="https://github.com/jimmyayoade/batowatch"
BATOWATCH_VERSION="${BATOWATCH_VERSION:-latest}"
INSTALL_DIR="/userdata/system/add-ons/batowatch"
SERVICE="/userdata/system/services/batowatch"
HISTORY_BACKUP="/userdata/system/batowatch_history"
PORTS_DIR="/userdata/roms/ports"
PORT_SCRIPT="${PORTS_DIR}/BatoWatch.sh"
PORT_IMAGE="${PORTS_DIR}/images/batowatch-logo.png"
GAME_LIST="${PORTS_DIR}/gamelist.xml"

APP_FILES="batowatch.py settings.py webauth.py tls.py history.py hardware.py alerts.py overlay.py known.py"
FLOWS_HELPER="bw-flows"     # per-connection packet counter, a release download

# The release's bw-flows for this machine, checked against the release's
# SHA256SUMS, as "${1}/bw-flows". Releases before 1.2.0 have none: then it
# returns 1 and BatoWatch uses the kernel's connection tracking.
download_flows_helper() {
    local dir="$1" asset sums
    asset="bw-flows-$(uname -m)"
    sums="$(curl -fsSL "${BATOWATCH_REPO}/releases/download/v${version}/SHA256SUMS" 2>/dev/null)" || return 1
    curl -fsSL -o "${dir}/${asset}" "${BATOWATCH_REPO}/releases/download/v${version}/${asset}" ||
        { echo "Failed to download ${asset}."; exit 1; }
    if ! (cd "${dir}" && printf '%s\n' "${sums}" | grep " ${asset}\$" | sha256sum -c --status); then
        echo "The ${asset} download doesn't match the release's checksum."
        exit 1
    fi
    mv "${dir}/${asset}" "${dir}/${FLOWS_HELPER}"
    chmod +x "${dir}/${FLOWS_HELPER}"
}

# One setting from batowatch.conf, read in a subshell as the service does,
# so the file's other settings stay there.
conf_value() {
    (
        set +eu
        # shellcheck disable=SC1091  # batowatch.conf is installed above
        . "${INSTALL_DIR}/batowatch.conf" >/dev/null 2>&1
        eval "printf '%s' \"\${$1}\""
    )
}

# The dashboard's addresses, one per line: "<ip>:<port>", plus "(Tailscale)"
# for the tailnet. Never with "https://": the BUA app turns any output line
# with a web address into an extra pop-up (and its dialog wrapper echoes the
# box text into the output). Typing the bare address in a browser works:
# BatoWatch redirects plain http to https.
dashboard_addresses() {
    local port bind ifname addr
    port="$(conf_value WEB_PORT)"; port="${port:-8686}"
    bind="$(conf_value WEB_BIND)"; bind="${bind:-0.0.0.0}"
    [ "${port}" = "0" ] && return 0

    # IPv4 address of every interface, skipping loopback and container bridges.
    while read -r ifname addr; do
        addr="${addr%/*}"
        case "${ifname}" in
            lo|docker*|br-*|veth*|virbr*|cni*) continue ;;
        esac
        if [ "${bind}" != "0.0.0.0" ] && [ "${bind}" != "${addr}" ]; then
            continue
        fi
        case "${ifname}" in
            tailscale*) echo "  ${addr}:${port}  (Tailscale)" ;;
            *)          echo "  ${addr}:${port}" ;;
        esac
    done < <(ip -o -4 addr show 2>/dev/null | awk '{print $2, $4}')
}

# The login, in two short lines
login_text() {
    case "$(conf_value LOGIN)" in
        ""|batocera) printf '%s\n' "Login:  root + your Batocera password" \
                                   "        (\"linux\" if you never set one)" ;;
        none)        echo "Login:  off (anyone who can reach it can open it)" ;;
        *)           echo "Login:  your own (set in batowatch.conf)" ;;
    esac
}


# The TV app in Ports: launcher, logo, and one gamelist entry. No .keys file:
# Batocera's own Hotkey+Start (KEY_EXIT) closes it.
add_ports_entry() {
    local tmp="$1"
    mkdir -p "${PORTS_DIR}/images"
    cat > "${PORT_SCRIPT}" << EOF
#!/bin/bash
# BatoWatch on the TV (installed by BatoWatch; removed by its uninstaller)
export DISPLAY=:0.0
exec python3 "${INSTALL_DIR}/tv/batowatch_tv.py"
EOF
    chmod +x "${PORT_SCRIPT}"
    cp "${src}/extra/batowatch_logo.png" "${PORT_IMAGE}"
    if [ ! -f "${GAME_LIST}" ]; then
        printf '<?xml version="1.0"?>\n<gameList>\n</gameList>\n' > "${GAME_LIST}"
    fi
    # Only add the entry when it isn't there yet, so a reinstall doesn't duplicate it
    if [ "$(xmlstarlet sel -t -v "count(/gameList/game[path='./BatoWatch.sh'])" "${GAME_LIST}" 2>/dev/null)" = "0" ]; then
        xmlstarlet ed -s "/gameList" -t elem -n "game" -v "" \
            -s "/gameList/game[last()]" -t elem -n "path" -v "./BatoWatch.sh" \
            -s "/gameList/game[last()]" -t elem -n "name" -v "BatoWatch" \
            -s "/gameList/game[last()]" -t elem -n "desc" -v "This box's hardware and network, live and with history." \
            -s "/gameList/game[last()]" -t elem -n "image" -v "./images/batowatch-logo.png" \
            "${GAME_LIST}" > "${GAME_LIST}.tmp" && mv "${GAME_LIST}.tmp" "${GAME_LIST}"
    fi
    curl -s http://127.0.0.1:1234/reloadgames >/dev/null 2>&1 || true
}

# main runs in a subshell, so its set and trap stay inside it. The BUA app
# sources this script and has its own EXIT trap, which would be replaced.
main() (
    set -euo pipefail
    local tmp src version f history_dir
    local restored="" added="" new_version old_version stopped="" has_flows

    if [ "$(uname -m)" != "x86_64" ]; then
        echo "BatoWatch is for x86_64 Batocera for now; ARM support comes later."
        exit 1
    fi
    if ! python3 -c "import sqlite3, ssl" 2>/dev/null; then
        echo "This Batocera's Python has no sqlite3 or ssl module, which BatoWatch needs."
        exit 1
    fi

    tmp="$(mktemp -d)"
    # If an update fails after BatoWatch was stopped, put the previous version's
    # files back and start it again, so the box isn't left without BatoWatch
    # shellcheck disable=SC2329  # run by the EXIT trap below
    on_exit() {
        local rc=$?
        if [ "${rc}" -ne 0 ] && [ -n "${stopped}" ]; then
            echo "The update failed; going back to the BatoWatch that was installed."
            cp -a "${tmp}/previous"/. "${INSTALL_DIR}/" 2>/dev/null || true
            cp -a "${tmp}/previous.service" "${SERVICE}" 2>/dev/null || true
            batocera-services start batowatch || true
        fi
        rm -rf "${tmp}"
    }
    trap on_exit EXIT

    # One download: the release's archive. "latest" is whatever the repo's
    # newest release is (GitHub's redirect, so no API rate limit).
    version="${BATOWATCH_VERSION#v}"
    if [ "${version}" = "latest" ]; then
        version="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "${BATOWATCH_REPO}/releases/latest" | sed -n 's#.*/tag/v##p')"
        [ -n "${version}" ] || { echo "Couldn't find BatoWatch's latest release."; exit 1; }
    fi
    echo "Downloading BatoWatch ${version}..."
    src="${tmp}/src"
    mkdir -p "${src}"
    curl -fsSL -o "${tmp}/batowatch.tar.gz" "${BATOWATCH_REPO}/archive/refs/tags/v${version}.tar.gz" ||
        { echo "Failed to download BatoWatch ${version} (is there a release called v${version}?)"; exit 1; }
    tar -xzf "${tmp}/batowatch.tar.gz" -C "${src}" --strip-components=1 ||
        { echo "The BatoWatch ${version} download is damaged."; exit 1; }
    has_flows=""
    download_flows_helper "${src}" && has_flows=1

    new_version="$(sed -n 's/^VERSION = "\(.*\)".*/\1/p' "${src}/batowatch.py")"
    old_version=""
    if [ -f "${INSTALL_DIR}/batowatch.py" ]; then
        old_version="$(sed -n 's/^VERSION = "\(.*\)".*/\1/p' "${INSTALL_DIR}/batowatch.py")"
    fi
    if [ -z "${old_version}" ] && [ -f "${INSTALL_DIR}/batowatch.py" ]; then
        echo "Updating BatoWatch to ${new_version}"
    elif [ -z "${old_version}" ]; then
        echo "Installing BatoWatch ${new_version}"
    elif [ "${old_version}" = "${new_version}" ]; then
        echo "Reinstalling BatoWatch ${new_version}"
    else
        echo "Updating BatoWatch ${old_version} -> ${new_version}"
    fi

    # Stop a running copy (an update) only once the download has succeeded.
    if [ -x "${SERVICE}" ]; then
        mkdir -p "${tmp}/previous"
        for f in ${APP_FILES} ${FLOWS_HELPER} uninstall.sh batowatch.conf aliases.conf www tv; do
            if [ -e "${INSTALL_DIR}/${f}" ]; then cp -a "${INSTALL_DIR}/${f}" "${tmp}/previous/"; fi
        done
        cp -a "${SERVICE}" "${tmp}/previous.service"
        batocera-services stop batowatch || true
        stopped=1
    fi

    echo "Installing to ${INSTALL_DIR}"
    mkdir -p "${INSTALL_DIR}/www" "${INSTALL_DIR}/tv" "$(dirname "${SERVICE}")"
    for f in ${APP_FILES}; do
        cp "${src}/${f}" "${INSTALL_DIR}/${f}"
    done
    cp "${src}"/www/* "${INSTALL_DIR}/www/"
    cp "${src}"/tv/* "${INSTALL_DIR}/tv/"
    cp "${src}/batowatch.conf" "${INSTALL_DIR}/batowatch.conf.default"
    chmod +x "${INSTALL_DIR}/batowatch.py" "${INSTALL_DIR}/overlay.py"
    # The packet counter, swapped in with mv: a running binary can't be overwritten.
    # Without it (an older release), a copy left by a newer one goes too.
    if [ -n "${has_flows}" ]; then
        cp "${src}/${FLOWS_HELPER}" "${INSTALL_DIR}/${FLOWS_HELPER}.new"
        mv "${INSTALL_DIR}/${FLOWS_HELPER}.new" "${INSTALL_DIR}/${FLOWS_HELPER}"
    else
        rm -f "${INSTALL_DIR}/${FLOWS_HELPER}"
    fi
    # The uninstaller, so BatoWatch can be removed without a download
    if [ -f "${src}/uninstall.sh" ]; then
        cp "${src}/uninstall.sh" "${INSTALL_DIR}/uninstall.sh"
        chmod +x "${INSTALL_DIR}/uninstall.sh"
    fi

    # Settings: never overwrite yours. A first install starts from the shipped
    # file; an update adds settings a newer version introduced.
    if [ -f "${INSTALL_DIR}/batowatch.conf" ]; then
        added="$(python3 "${INSTALL_DIR}/settings.py" add-missing "${INSTALL_DIR}/batowatch.conf" \
                 "${src}/batowatch.conf" | python3 -c 'import json,sys; print(", ".join(json.load(sys.stdin)["added"]))')"
    else
        cp "${src}/batowatch.conf" "${INSTALL_DIR}/batowatch.conf"
        chmod 600 "${INSTALL_DIR}/batowatch.conf"
    fi
    if [ ! -f "${INSTALL_DIR}/aliases.conf" ]; then
        cp "${src}/aliases.conf" "${INSTALL_DIR}/aliases.conf"
    fi

    # History kept by an earlier uninstall comes back
    history_dir="$(conf_value HISTORY_DIR)"
    history_dir="${history_dir:-${INSTALL_DIR}/history}"
    if [ -d "${HISTORY_BACKUP}" ] && [ ! -e "${history_dir}/network.db" ] && \
       [ ! -e "${history_dir}/hardware.db" ]; then
        mkdir -p "${history_dir}"
        cp -a "${HISTORY_BACKUP}"/. "${history_dir}/"
        if python3 - "${history_dir}" << 'EOF'
import os, sqlite3, sys
for name in ("network.db", "hardware.db"):
    db = os.path.join(sys.argv[1], name)
    try:
        if os.path.exists(db) and \
                sqlite3.connect(db).execute("PRAGMA quick_check").fetchone()[0] != "ok":
            sys.exit(1)
    except sqlite3.Error:
        sys.exit(1)
EOF
        then
            restored="$(du -ch "${history_dir}"/network.db* "${history_dir}"/hardware.db* 2>/dev/null | tail -n 1 | cut -f1)"
            rm -rf "${HISTORY_BACKUP}"
        else
            echo "The kept history in ${HISTORY_BACKUP} looks damaged; it was left there and BatoWatch starts a new one."
            rm -f "${history_dir}"/network.db* "${history_dir}"/hardware.db*
        fi
    fi

    # Point the service at INSTALL_DIR, in case it was changed above.
    sed "s#^BATOWATCH_HOME=.*#BATOWATCH_HOME=\"\${BATOWATCH_HOME:-${INSTALL_DIR}}\"#" \
        "${src}/batowatch-service.sh" > "${SERVICE}"
    chmod +x "${SERVICE}"

    batocera-services enable batowatch
    batocera-services start batowatch

    # The TV app in Ports (a reinstall updates it, never duplicates it)
    add_ports_entry "${tmp}"

    # One short summary box. The BUA app shows it at the end; the log gets the
    # same text with bare addresses (see dashboard_addresses).
    local notes=() port https head links
    [ -n "${restored}" ] && notes+=("Restored your history (${restored}).")
    [ -n "${added}" ] && notes+=("New settings in batowatch.conf: ${added}.")
    [ -n "${has_flows}" ] || notes+=("This release has no packet counter; the Network tab uses the kernel's connection tracking.")
    port="$(conf_value WEB_PORT)"
    https="$(conf_value HTTPS)"
    head="In a browser, go to:"
    links="$(dashboard_addresses)"
    [ "${https:-self}" != "off" ] && links+=$'\n'"  (it warns once about its certificate; accept it)"
    {
        echo ""
        echo "BatoWatch ${new_version} is installed."
        printf '%s\n' "${notes[@]}"
        echo "On the TV:   Ports > BatoWatch"
        if [ "${port}" != "0" ]; then
            echo "${head}"
            echo "${links}"
            login_text
        fi
        echo "Settings:    ${INSTALL_DIR}/batowatch.conf"
        [ -f "${INSTALL_DIR}/uninstall.sh" ] && echo "Uninstall:   ${INSTALL_DIR}/uninstall.sh"
    }
    local box="BatoWatch ${new_version} is installed."
    [ "${#notes[@]}" -gt 0 ] && box+="\n$(printf '%s\n' "${notes[@]}")"
    box+="\n\nOn the TV:   Ports > BatoWatch"
    if [ "${port}" != "0" ]; then
        box+="\n\n${head}\n${links}\n\n$(login_text)"
    else
        box+="\n\nThe dashboard is turned off (WEB_PORT=0)."
    fi
    # (no screen, e.g. `bua add` over SSH: dialog just says so; that's not worth a line)
    dialog --title "BatoWatch" --clear --timeout 300 --msgbox "${box}" 0 0 2>/dev/null || true
)

# Called on the last line so the whole script is downloaded before it runs.
main "$@"
