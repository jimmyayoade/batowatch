# Changelog

Each version is a git tag (`v1.0.0`) and a release on GitHub.
Versions follow [semantic versioning](https://semver.org): bug fixes raise the
last number, new features the middle one, and changes that need you to do
something the first one.

## 1.2.0 (2026-11-18)

- **Packet counter (`bw-flows`):** a small helper, built for x86_64 and
  aarch64 and attached to each release, counts each connection's traffic.
  Batocera 43 has no kernel connection tracking, so the Network tab needs it
  there. It uses about 0.1% of one core, against about 5% for doing the same
  in Python.
- **Unknown-device alert:** a device BatoWatch doesn't know that connects to
  the box gets a red UNKNOWN DEVICE banner, a line in the Activity list and
  the log. Known means in `aliases.conf` or one of your Tailscale machines.
  *Mark as known* and *Forget* on the dashboard (and X in the TV app);
  settings `ALERT_UNKNOWN`, `ALERT_ATTEMPTS`, `KNOWN_DEVICES` and
  `NOTIFY=both`. The first time, the home-network devices already in the
  history are added to `aliases.conf`, so your own devices don't alert.
- **A gauge for every GPU:** a box with two GPUs showed one gauge, for
  whichever card came first. Each GPU now has its own tile (load gauge;
  temperature, power and memory), its own line in the GPU graphs and, in the
  TV app, its own section and page. The NVIDIA card no longer shows twice in
  the GPU card. The top row scrolls sideways when the tiles don't fit (left
  and right in the TV app).
- **Banners leave no trails:** over a screen that doesn't redraw, like an
  idle EmulationStation, the slide-in left trails and the text stayed after
  the banner closed. Fixed.
- **Tailscale relays** are recognised by address, so they no longer show up
  as unknown internet devices. The dashboard's own port is labelled
  batowatch-web.
- **Own installer and uninstaller** (`install.sh`, `uninstall.sh`): install
  or update with one command; the uninstaller is put on the box. With no
  screen to ask on, the uninstaller keeps the history.

## 1.1.0 (2026-11-08)

- **TV app with a keyboard:** Esc exits (like Hotkey + Start), Backspace goes
  back, Tab / Shift+Tab switch between Hardware and Network, Space opens
  Settings and R refreshes. The hints at the bottom show keyboard keys when no
  controller is plugged in.
- **TV app, Start opens Settings** (it refreshed before); Back (Select) now
  refreshes. Readings refresh by themselves every 2 seconds anyway.

## 1.0.0 (2026-11-07)

First release. Replaces NetWatch.

- **Hardware:** CPU, GPU, memory, power, disks (space, activity, health and
  temperature), temperatures and fans, read from whatever the box has
  (Intel, AMD and NVIDIA). Parts a box doesn't have are listed with the
  reason, never a crash.
- **Network:** who is connected and who is streaming with Moonlight, over the
  home network and Tailscale, carried over from NetWatch.
- **History:** every reading for 24 hours, minute averages for 7 days, hour
  averages for a year; about 7 MB of disk writes a day.
- **Alerts:** on-screen banners for a hot CPU, GPU or disk, a disk almost full
  or failing, and memory almost full; Moonlight connect banners.
- **Web dashboard:** Hardware, Network and Settings tabs, on any phone, tablet
  or computer; login with the Batocera root password, HTTPS by default.
- **TV app:** in Ports, with controller or keyboard, and an animated loading
  screen.
- **Install:** takes over an installed NetWatch (settings and device names
  kept); uninstalling asks whether to keep the history.
