#!/bin/sh
# Container start for the Railway image (root Dockerfile), ADR-009 section 6.
# Lives in the module (not docker/, which .dockerignore excludes from the build context).
#
# 1. Run the flex image's own setup (openemr.sh) with FLEX_SKIP_APACHE_EXEC=yes,
#    so it finishes setup and returns instead of starting Apache. A setup failure
#    stops the container exactly as it did before this script existed.
# 2. As the web user, run the Co-Pilot module's migration runner (never OpenEMR's
#    openemr:zfc-module CLI, which resolves the wrong module). It is bounded by a
#    timeout, and any failure is reported as a code and ignored: the module keeps
#    document processing disabled until its tables exist.
# 3. Patient dashboard (React), modes B/C: when DASHBOARD_SMART_CLIENT_ID is set
#    (after the SMART client is registered), write the module's
#    public/dashboard.config.json. Only a plain client id (8-128 of A-Z a-z 0-9 _ -)
#    is written; anything else is reported and skipped.
# 4. exec Apache in the foreground, always.
#
# If the base image's openemr.sh ignored FLEX_SKIP_APACHE_EXEC it would exec
# Apache itself and never return: Apache still starts, only the migration is
# skipped (the module then reports copilot_tables_not_installed).
#
# Output is fixed codes only. Overrides (used by the module's StartScriptTest):
#   COPILOT_OPENEMR_SH, COPILOT_MIGRATE_CMD, COPILOT_HTTPD_CMD, COPILOT_MIGRATE_TIMEOUT,
#   COPILOT_OPENEMR_ROOT, COPILOT_DASHBOARD_CONFIG
set -u

OPENEMR_SH="${COPILOT_OPENEMR_SH:-/var/www/localhost/htdocs/openemr.sh}"
HTTPD_CMD="${COPILOT_HTTPD_CMD:-/usr/sbin/httpd}"
MIGRATE_TIMEOUT="${COPILOT_MIGRATE_TIMEOUT:-300}"
OPENEMR_ROOT="${COPILOT_OPENEMR_ROOT:-/var/www/localhost/htdocs/openemr}"
MIGRATE_PHP="$OPENEMR_ROOT/interface/modules/custom_modules/oe-module-copilot/bin/copilot-migrate.php"
DASHBOARD_CONFIG="${COPILOT_DASHBOARD_CONFIG:-$OPENEMR_ROOT/interface/modules/custom_modules/oe-module-copilot/public/dashboard.config.json}"

cd "$(dirname "$OPENEMR_SH")" || exit 1
FLEX_SKIP_APACHE_EXEC=yes "$OPENEMR_SH"
setup_rc=$?
if [ "$setup_rc" -ne 0 ]; then
    echo "copilot-start: openemr_setup_exit_$setup_rc"
    exit "$setup_rc"
fi

if [ -n "${COPILOT_MIGRATE_CMD:-}" ]; then
    set -- "$COPILOT_MIGRATE_CMD"
elif [ -f "$MIGRATE_PHP" ] && command -v su-exec >/dev/null 2>&1; then
    # The flex image's own way to run OpenEMR CLI scripts as apache (openemr.sh, auto_configure).
    set -- su-exec apache php "$MIGRATE_PHP"
elif [ -f "$MIGRATE_PHP" ]; then
    set -- su -s /bin/sh apache -c "php '$MIGRATE_PHP'"
else
    set --
    echo "copilot-start: migrate_script_missing"
fi

if [ "$#" -gt 0 ]; then
    if command -v timeout >/dev/null 2>&1; then
        timeout "$MIGRATE_TIMEOUT" "$@"
    else
        "$@"
    fi
    migrate_rc=$?
    if [ "$migrate_rc" -ne 0 ]; then
        echo "copilot-start: migrate_exit_$migrate_rc"
    fi
fi

if [ -n "${DASHBOARD_SMART_CLIENT_ID:-}" ]; then
    id="$DASHBOARD_SMART_CLIENT_ID"
    case "$id" in
        *[!A-Za-z0-9_-]*) valid=no ;;
        *) valid=yes ;;
    esac
    if [ "$valid" = yes ] && [ "${#id}" -ge 8 ] && [ "${#id}" -le 128 ]; then
        if printf '{"clientId":"%s"}\n' "$id" > "$DASHBOARD_CONFIG"; then
            echo "copilot-start: dashboard_config_written"
        else
            echo "copilot-start: dashboard_config_failed"
        fi
    else
        echo "copilot-start: dashboard_client_id_invalid"
    fi
fi

exec "$HTTPD_CMD" -D FOREGROUND
