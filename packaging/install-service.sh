#!/bin/sh
# Installs (or removes) usb-tracker as a per-user background recorder with a
# panel icon, plus a launcher entry for the window.
#
#   ./packaging/install-service.sh              install and start both
#   ./packaging/install-service.sh --uninstall  remove them again
#
# Per-user, so nothing here needs root: the units go in
# ~/.config/systemd/user, the autostart entry in ~/.config/autostart and the
# launcher in ~/.local/share/applications. The recorded history is left alone
# either way.
#
# Two units, because they want different things from the session:
#
#   usb-tracker.service       headless, enabled against default.target, so it
#                             records from boot and across logins.
#   usb-tracker-tray.service  needs a display and a tray, so the session
#                             starts it from the autostart entry and it has no
#                             [Install] section at all. See the unit file.
set -eu

APPDIR=$(cd "$(dirname "$0")/.." && pwd)
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
RECORDER=usb-tracker.service
TRAY=usb-tracker-tray.service
AUTOSTART=usb-tracker-tray-autostart.desktop
DESKTOP=usb-tracker.desktop
INTERVAL=${USB_TRACKER_INTERVAL:-1}

say() { printf '%s\n' "$*"; }

if [ "${1:-}" = "--uninstall" ] || [ "${1:-}" = "-u" ]; then
    for unit in "$TRAY" "$RECORDER"; do
        systemctl --user stop "$unit" 2>/dev/null || true
        systemctl --user disable "$unit" 2>/dev/null || true
    done
    rm -f "$UNIT_DIR/$RECORDER" "$UNIT_DIR/$TRAY" \
          "$AUTOSTART_DIR/$AUTOSTART" "$APPS_DIR/$DESKTOP"
    systemctl --user daemon-reload
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$APPS_DIR" 2>/dev/null || true
    say "Removed both units, the autostart entry and the launcher."
    say "Your recorded history is untouched (~/.local/share/usb-tracker)."
    exit 0
fi

PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
    say "No python3 on PATH."
    exit 1
fi

# Say up front what will and will not work, rather than leaving it to be
# discovered as a unit that keeps restarting.
"$PYTHON" - <<'EOF' || true
# enumerate_versions, not require_version: requiring GTK 4 and then GTK 3 in
# one process always fails, which would report the panel icon as unavailable
# on a machine where it works perfectly well.
from gi import Repository
have = Repository.get_default()
for name, version, what in (("Gtk", "4.0", "the window"),
                            ("Gtk", "3.0", "the panel icon"),
                            ("AyatanaAppIndicator3", "0.1", "the panel icon")):
    if version not in (have.enumerate_versions(name) or []):
        print(f"  missing {name} {version} -- {what} will not start")
EOF

mkdir -p "$UNIT_DIR" "$AUTOSTART_DIR" "$APPS_DIR"
subst() {
    sed -e "s|@APPDIR@|$APPDIR|g" -e "s|@PYTHON@|$PYTHON|g" \
        -e "s|@INTERVAL@|$INTERVAL|g" "$1" > "$2"
}
subst "$APPDIR/packaging/$RECORDER.in"   "$UNIT_DIR/$RECORDER"
subst "$APPDIR/packaging/$TRAY.in"       "$UNIT_DIR/$TRAY"
subst "$APPDIR/packaging/$AUTOSTART.in"  "$AUTOSTART_DIR/$AUTOSTART"
subst "$APPDIR/packaging/$DESKTOP.in"    "$APPS_DIR/$DESKTOP"

systemctl --user daemon-reload

# The recorder is headless, so enabling it is right: it starts at boot where
# the user manager lingers, and keeps recording across logins.
systemctl --user enable "$RECORDER" >/dev/null
systemctl --user restart "$RECORDER"

# The tray must never be enabled -- an older install may have been, so undo
# that -- and the session is what starts it. Importing the display here starts
# it now, without having to log out first.
systemctl --user disable "$TRAY" 2>/dev/null || true
if [ -n "${DISPLAY:-}" ]; then
    systemctl --user import-environment DISPLAY XAUTHORITY || true
    systemctl --user restart "$TRAY"
else
    say "No DISPLAY here, so the panel icon was not started; it will appear"
    say "when you next log in."
fi

command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$APPS_DIR" 2>/dev/null || true

say ""
say "Installed: $UNIT_DIR/$RECORDER"
say "           $UNIT_DIR/$TRAY"
say "           $AUTOSTART_DIR/$AUTOSTART  (starts the icon with your session)"
say "           $APPS_DIR/$DESKTOP"
say ""
say "The recorder polls every ${INTERVAL}s and runs from boot. The panel icon"
say "shows what it has seen; its first menu item opens the window."
say "Remove both again with: ./packaging/install-service.sh --uninstall"
say ""
for unit in "$RECORDER" "$TRAY"; do
    systemctl --user --no-pager --lines=0 status "$unit" 2>/dev/null \
        | head -3 || true
done
