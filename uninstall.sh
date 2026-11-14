#!/bin/bash
# BatoWatch uninstaller for Batocera. Removes BatoWatch, its service, its
# settings, its logs (including logs moved elsewhere in batowatch.conf), its
# Ports entry and its uninstaller.
#
# Asks first whether to keep the history. Yes (the button the BUA app
# selects first) keeps it in /userdata/system/batowatch_history, where a later
# install picks it up; No deletes it. No answer (a timeout)
# also keeps it, as does running it with no terminal (e.g. ssh without -t).
#   /userdata/system/add-ons/batowatch/uninstall.sh       (the installer puts it there)
#   curl -L https://raw.githubusercontent.com/jimmyayoade/batowatch/main/uninstall.sh | bash
# Without questions, deleting the history too (LB+RB in the BUA app):
#   uninstall.sh --ignore-warning     (or: curl -L <same url> | bash -s -- --ignore-warning)

INSTALL_DIR="/userdata/system/add-ons/batowatch"
SERVICE="/userdata/system/services/batowatch"
HISTORY_BACKUP="/userdata/system/batowatch_history"
PORTS_DIR="/userdata/roms/ports"
PORT_SCRIPT="${PORTS_DIR}/BatoWatch.sh"
PORT_IMAGE="${PORTS_DIR}/images/batowatch-logo.png"
GAME_LIST="${PORTS_DIR}/gamelist.xml"

ignore_warning=false   # set to true by --ignore-warning in main

main() {
    local log slog history_dir size
    for arg in "$@"; do
        case "${arg}" in
            --ignore-warning) ignore_warning=true ;;
            *) echo "Usage: uninstall.sh [--ignore-warning]"; exit 2 ;;
        esac
    done

    # Read batowatch.conf the way the service does, to find the logs and history.
    read -r log slog history_dir < <(
        set +eu
        LOG=""
        SERVICE_LOG=""
        HISTORY_DIR=""
        # shellcheck disable=SC1091
        [ -f "${INSTALL_DIR}/batowatch.conf" ] && . "${INSTALL_DIR}/batowatch.conf" >/dev/null 2>&1
        LOG="${LOG:-/userdata/system/logs/batowatch.log}"
        SERVICE_LOG="${SERVICE_LOG:-$(dirname "${LOG}")/batowatch-service.log}"
        HISTORY_DIR="${HISTORY_DIR:-${INSTALL_DIR}/history}"
        printf '%s %s %s\n' "${LOG}" "${SERVICE_LOG}" "${HISTORY_DIR}"
    )

    echo "Stopping BatoWatch..."
    batocera-services stop batowatch 2>/dev/null
    batocera-services disable batowatch 2>/dev/null
    [ -x "${SERVICE}" ] && "${SERVICE}" stop >/dev/null 2>&1   # in case batocera-services didn't

    # The history: delete it, or keep it for a later install. Only BatoWatch's
    # own files: HISTORY_DIR can be a folder that holds other things too.
    local name suffix
    local -a files=()
    for name in network hardware; do
        for suffix in .db .db-wal .db-shm .broken .broken-wal .broken-shm; do
            [ -e "${history_dir}/${name}${suffix}" ] && files+=("${history_dir}/${name}${suffix}")
        done
    done
    if [ "${#files[@]}" -gt 0 ]; then
        size="$(du -ch "${files[@]}" | tail -n 1 | cut -f1)"
        echo "BatoWatch's history: ${size} in ${history_dir}"
        # Keeping is the default: only a real No, or --ignore-warning (LB+RB
        # removes everything without asking), deletes it. dialog returns 1
        # for No and 255 for a timeout, which keeps it. With no terminal it
        # also returns 1 straight away, so it is only asked where it can be
        # answered: in the BUA app (its dialog wrapper is a shell function)
        # or on a terminal.
        local answer=0
        if [ "${ignore_warning}" = "true" ]; then
            answer=1
            echo "Deleting the history without asking (--ignore-warning)."
        elif [ "$(type -t dialog)" != "function" ] && ! { : < /dev/tty; } 2>/dev/null; then
            echo "No screen to ask on, so the history is kept (--ignore-warning deletes it)."
        else
            dialog --title "Uninstall BatoWatch" --clear --timeout 60 --yesno \
                "Keep BatoWatch's history (${size})?\n\nYes keeps it in ${HISTORY_BACKUP}, and installing BatoWatch again picks it up.\nNo deletes it." 0 0
            answer=$?
        fi
        if [ "${answer}" -eq 1 ]; then
            rm -f "${files[@]}"
            echo "History deleted."
        else
            rm -rf "${HISTORY_BACKUP}"
            mkdir -p "${HISTORY_BACKUP}"
            mv "${files[@]}" "${HISTORY_BACKUP}/"
            echo "History kept in ${HISTORY_BACKUP} (${size})."
        fi
    fi

    # The Ports entry. ES keeps a played game's stats in memory and writes them
    # back to gamelist.xml on the next reload, which brings back the entry
    # removed below. Removing the game through ES first, while its launcher
    # still exists, drops it from memory.
    if [ -f "${PORT_SCRIPT}" ] || grep -q "./BatoWatch.sh" "${GAME_LIST}" 2>/dev/null; then
        echo "Removing BatoWatch from Ports..."
        curl -s -X POST "http://127.0.0.1:1234/removegames/ports" \
            -d "<gameList><game><path>${PORT_SCRIPT}</path></game></gameList>" >/dev/null 2>&1
        rm -f "${PORT_SCRIPT}" "${PORT_IMAGE}"
        if [ -f "${GAME_LIST}" ]; then
            xmlstarlet ed -d "/gameList/game[path='./BatoWatch.sh']" "${GAME_LIST}" > "${GAME_LIST}.tmp" \
                && mv "${GAME_LIST}.tmp" "${GAME_LIST}"
        fi
        curl -s http://127.0.0.1:1234/reloadgames >/dev/null 2>&1
    fi

    echo "Removing BatoWatch..."
    rm -f "${SERVICE}" /var/run/batowatch.pid
    rm -f "${log}" "${log}".[0-9]* "${slog}" "${slog}".[0-9]*
    rm -rf "${INSTALL_DIR}"

    echo "BatoWatch has been removed."
    return 0
}

main "$@"
exit $?
