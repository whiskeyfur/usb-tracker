#!/bin/sh
# Convenience launcher: ./run.sh [--daemon|--list|--events] [options]
cd "$(dirname "$0")" && exec python3 -m usbtracker "$@"
