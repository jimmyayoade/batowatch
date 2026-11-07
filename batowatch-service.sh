#!/bin/bash
#
# BatoWatch service for Batocera.
# Installed as /userdata/system/services/batowatch (no extension). Enable it
# from EmulationStation (System settings > Services) or with
# `batocera-services enable batowatch`.
#
# Settings live in $BATOWATCH_HOME/batowatch.conf, which batowatch.py reads
# itself. This script only takes the log settings from it.

BATOWATCH_HOME="${BATOWATCH_HOME:-/userdata/system/add-ons/batowatch}"
CONF="${BATOWATCH_HOME}/batowatch.conf"
PIDFILE="/var/run/batowatch.pid"

# Defaults; batowatch.conf may override any of these.
LOG=""                  # empty means /userdata/system/logs/batowatch.log
LOG_MAX_BYTES=1048576   # the service log rolls over at this size too
SERVICE_LOG=""          # empty means batowatch-service.log beside LOG

read_conf() {
    # shellcheck disable=SC1090  # the user's own settings file
    [ -f "${CONF}" ] && . "${CONF}"
    LOG="${LOG:-/userdata/system/logs/batowatch.log}"
    SERVICE_LOG="${SERVICE_LOG:-$(dirname "${LOG}")/batowatch-service.log}"
    case "${LOG_MAX_BYTES}" in ''|*[!0-9]*) LOG_MAX_BYTES=1048576 ;; esac
}

running() {
    [ -f "${PIDFILE}" ] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null
}

start() {
    if running; then
        echo "BatoWatch is already running (pid $(cat "${PIDFILE}"))"
        return 0
    fi
    if [ ! -f "${BATOWATCH_HOME}/batowatch.py" ]; then
        echo "batowatch.py not found in ${BATOWATCH_HOME}"
        return 1
    fi
    read_conf
    mkdir -p "$(dirname "${LOG}")" "$(dirname "${SERVICE_LOG}")"
    # batowatch.py rotates its own log (LOG). The service log only catches
    # restarts and anything printed before BatoWatch's logging is up (e.g. a
    # syntax error). The loop reopens it on every pass and rolls it to .1 once
    # it passes LOG_MAX_BYTES, so even a crash loop stays bounded.
    # setsid gives the loop its own process group, so stop can end the loop and
    # python together. The loop restarts BatoWatch if it exits: at once when
    # BatoWatch asked for it (exit 75, a setting that needs a restart),
    # otherwise after 5 s.
    # The loop first closes every file it inherited above stderr: whoever ran
    # start may still be reading one of them (the BUA app reads the install's
    # output through fd 9) and would wait for BatoWatch to exit before finishing.
    # shellcheck disable=SC2016  # the loop's variables expand in the inner bash, on purpose
    BATOWATCH_HOME="${BATOWATCH_HOME}" DISPLAY="${DISPLAY:-:0}" \
    LOG_MAX_BYTES="${LOG_MAX_BYTES}" SLOG="${SERVICE_LOG}" \
    setsid bash -c '
        for fd in "/proc/$$/fd"/*; do
            fd="${fd##*/}"
            [ "$fd" -gt 2 ] && eval "exec ${fd}>&-"
        done
        while true; do
            if [ "$LOG_MAX_BYTES" -gt 0 ] && [ -f "$SLOG" ] && \
               [ "$(stat -c %s "$SLOG")" -gt "$LOG_MAX_BYTES" ]; then
                mv -f "$SLOG" "$SLOG.1"
            fi
            python3 "$BATOWATCH_HOME/batowatch.py" --no-tui >>"$SLOG" 2>&1
            rc=$?
            if [ "$rc" -eq 75 ]; then
                echo "$(date "+%F %T") BatoWatch restarting (settings changed)" >>"$SLOG"
                continue
            fi
            echo "$(date "+%F %T") BatoWatch exited with status $rc; restarting in 5s" >>"$SLOG"
            sleep 5
        done' >/dev/null 2>&1 < /dev/null &
    echo $! > "${PIDFILE}"
    echo "BatoWatch started (pid $!), log: ${LOG}"
}

stop() {
    if running; then
        local pid
        pid="$(cat "${PIDFILE}")"
        # TERM lets BatoWatch save its history before it exits. Wait for that
        # (a few seconds at most; 30 on a slow SD card), so whatever runs next
        # (start, the installer, the uninstaller) finds it finished
        kill -TERM -- "-${pid}" 2>/dev/null || kill "${pid}"
        for _ in $(seq 1 60); do
            kill -0 -- "-${pid}" 2>/dev/null || break
            sleep 0.5
        done
        kill -KILL -- "-${pid}" 2>/dev/null || true
        rm -f "${PIDFILE}"
        echo "BatoWatch stopped"
    else
        rm -f "${PIDFILE}"
        echo "BatoWatch is not running"
    fi
}

case "$1" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    status)
        read_conf
        if running; then echo "BatoWatch is running (pid $(cat "${PIDFILE}")), log: ${LOG}"
        else echo "BatoWatch is stopped"; exit 1; fi ;;
    *) echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac
