#!/bin/sh
# Installs the privileged helper and its polkit action.
#
# Without this, the app still works: it falls back to running the same write
# through plain pkexec, which asks for an admin password every single time.
# With it, the authentication is scoped to this one operation and remembered
# for the session.
set -eu

HELPER_DIR=/usr/libexec/usb-tracker
HELPER=$HELPER_DIR/usb-tracker-power-helper
ACTIONS=/usr/share/polkit-1/actions
POLICY=$ACTIONS/dev.local.usbtracker.policy

here=$(cd "$(dirname "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "This installs two files as root:"
    echo "  $HELPER"
    echo "  $POLICY"
    echo
    echo "Re-running under sudo..."
    exec sudo "$0" "$@"
fi

install -d -m 0755 "$HELPER_DIR"
install -m 0755 "$here/usb-tracker-power-helper" "$HELPER"
install -m 0644 "$here/dev.local.usbtracker.policy" "$POLICY"

echo "Installed:"
echo "  $HELPER"
echo "  $POLICY"
echo
echo "To remove: sudo rm -f $HELPER $POLICY && sudo rmdir $HELPER_DIR"
