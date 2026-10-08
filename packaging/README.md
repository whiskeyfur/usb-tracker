# packaging

| File | What it is |
| --- | --- |
| `install-service.sh` | Installs both user units, the autostart entry and a launcher. `--uninstall` removes them. |
| `usb-tracker.service.in` | The recorder: headless, enabled, runs from boot. |
| `usb-tracker-tray.service.in` | The panel icon: needs a display, so it has no `[Install]` section. |
| `usb-tracker-tray-autostart.desktop.in` | What actually starts the icon, from inside the session. |
| `usb-tracker.desktop.in` | Launcher entry for the window. |
| `usb-tracker-power-helper` | Sets one validated device's `power/control`. Runs as root through polkit. |
| `dev.local.usbtracker.policy` | The polkit action that authorises the helper, with `auth_admin_keep`. |
| `install-helper.sh` | Installs those two, prints how to undo it. |

The `.in` files are templates; the installer fills in the checkout path and
the python3 it found.

## Record in the background, with a panel icon

```bash
./packaging/install-service.sh
```

Nothing here needs root. It writes two units to `~/.config/systemd/user`, an
autostart entry to `~/.config/autostart` and a launcher to
`~/.local/share/applications`, then starts both. `--uninstall` removes all of
it and leaves your recorded history alone.

Set the recorder's poll interval with `USB_TRACKER_INTERVAL=0.5` in the
environment when you install; the default is 1 second.

Then check what it has seen:

```bash
systemctl --user status usb-tracker usb-tracker-tray
journalctl --user -u usb-tracker -f
python3 -m usbtracker --outages
```

The window reads the same database, so it shows that history too.

## Two units, because they need different things

The recorder is headless. It is enabled against `default.target`, so where
the user manager lingers it starts at boot and keeps recording across logout
and login — which is the whole point of having it.

The panel icon needs an X display and a tray to appear in, and neither target
delivers that: `graphical-session.target` is never activated by some desktops
(Cinnamon among them), so a unit wanted by it enables, reports `enabled` and
never starts; and `default.target` is reached at boot, before any session
exists, so the icon would start display-less, fail, and retry in a loop.

So the icon's unit has no `[Install]` section — do not enable it — and the
autostart entry starts it from inside the session, handing systemd the
display first. Each unit file says this in its own header.
