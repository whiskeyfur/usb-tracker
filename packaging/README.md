# packaging

| File | What it is |
| --- | --- |
| `usb-tracker-power-helper` | Sets one validated device's `power/control`. Runs as root through polkit. |
| `dev.local.usbtracker.policy` | The polkit action that authorises the helper, with `auth_admin_keep`. |
| `install-helper.sh` | Installs those two, prints how to undo it. |
| `usb-tracker.service` | A user service that records continuously, for catching intermittent faults. |

## Record in the background

An outage that happens twice a day is only caught by something that is always
running. Install the user service:

```bash
mkdir -p ~/.config/systemd/user
cp packaging/usb-tracker.service ~/.config/systemd/user/
# edit WorkingDirectory in that file if your checkout is elsewhere
systemctl --user daemon-reload
systemctl --user enable --now usb-tracker.service
```

Then check what it has seen:

```bash
python3 -m usbtracker --outages
```

The window reads the same database, so it shows that history too.
