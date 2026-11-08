# Changelog

Each version is a git tag (`v1.0.0`) and a release on GitHub.
Versions follow [semantic versioning](https://semver.org): bug fixes raise the
last number, new features the middle one, and changes that need you to do
something the first one.

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
