# bt-controller

Turn the `R2` Bluetooth ring (a TikTok-scroll remote that masquerades as a
touchpad/mouse) into a 6-button macro remote. The daemon grabs the ring's input
nodes so the host never sees cursor/scroll events, decodes the synthetic swipe
gestures and media keys, and re-emits them as keyboard combos
(`Ctrl+Shift+Super+F1..F12`) that you bind in sway.

## How the ring reports input

The ring exposes three input nodes:

| node | what it sends |
|---|---|
| `R2` | synthetic single-touch swipes (`ABS_MT_*` + `BTN_TOUCH`) |
| `R2 Consumer Control` | media keys (`KEY_VOLUMEUP/DOWN`, `KEY_VIDEO_NEXT`) |
| `R2 Keyboard` | silent so far |

Buttons are reported as touch gestures, one axis per gesture:

- **direction buttons** → fast swipe (short press) or slow drift (long press)
- **b1** → ultra-fast "flick" down
- **b2** → long press = `KEY_VIDEO_NEXT`; short press duplicates right-long

### Classification

Gestures are classified by travel and speed (see `mapping.yaml`):

| class | rule | example |
|---|---|---|
| `tap` | `travel < tap_travel` | b1 sometimes |
| `flick` | `speed >= flick_speed` | b1 short (~34000 u/s) |
| `short` | `speed >= short_speed` | direction swipes (~7000-15000 u/s) |
| `long` | otherwise | slow drift (~800 u/s) |

Long presses may be split by the ring into several contacts; the daemon
coalesces them (`long_quiet_ms`) and fires once.

Held media keys (left/right long press) auto-repeat on the ring itself; the
daemon suppresses repeats of the same source key within `hold_repeat_ms` so a
hold fires once, matching the up/down long-press behavior.

## Files

- `r2_controller.py` — the daemon (`learn`, `run`, `capture` modes)
- `mapping.yaml` — signal → key bindings
- `r2-controller.service` — systemd user unit
- `60-r2-controller.rules` — udev rule for `/dev/uinput` + R2 nodes
- `uinput.conf` — loads the `uinput` module at boot

## Requirements

- `python3-evdev`, `PyYAML`
- Access to `/dev/input/event*` and write access to `/dev/uinput`
  (membership in the `input` group + the udev rule)

## Setup

```bash
# udev rule (grants /dev/uinput to the input group + active seat via uaccess)
sudo cp 60-r2-controller.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules

# optional: group-based fallback (uaccess already covers the seat user)
sudo usermod -aG input "$USER"          # re-login or `newgrp input`

# load uinput now and on every boot (the udev rule only fires once the
# uinput module is loaded; without this /dev/uinput stays root:root)
sudo cp uinput.conf /etc/modules-load.d/uinput.conf
sudo modprobe uinput
ls -l /dev/uinput                       # expect: crw-rw----. root input
```

Notes:
- The udev rule is only applied when the `uinput` device is added, i.e. when the
  module loads. `uinput.conf` makes that happen at boot.
- `TAG+="uaccess"` gives the active seat user access via a logind ACL, so the
  systemd user service works even if it doesn't have the `input` group.
  (`SupplementaryGroups=` does **not** work in `systemd --user` services — it
  fails with `status=216/GROUP`.)

## Ring connection

Trust the ring so it reconnects automatically:

```bash
bluetoothctl trust <MAC>
```

The daemon waits for the ring and grabs it whenever it connects.

## Usage

```bash
# interactive calibration -> writes mapping.yaml, prints sway snippet
python3 r2_controller.py learn

# run the remapper
python3 r2_controller.py run

# raw event dump (debug)
python3 r2_controller.py capture
```

`learn` walks through each button (short then long). Press `Enter` on the real
keyboard to skip a press, `Esc` to quit. It auto-dedupes identical signals and
adds a `tap` fallback for flicks.

### Autostart

```bash
mkdir -p ~/.config/systemd/user
cp r2-controller.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now r2-controller.service
```

## Tests

```bash
python3 -m unittest test_r2_controller -v
```

## Sway bindings

Add to `~/.config/sway/config.d/` and `swaymsg reload`:

```ini
bindsym Ctrl+Shift+Mod4+F1  exec notify-send -t 800 "R2" "up short"
bindsym Ctrl+Shift+Mod4+F2  exec notify-send -t 800 "R2" "up long"
bindsym Ctrl+Shift+Mod4+F3  exec notify-send -t 800 "R2" "down short"
bindsym Ctrl+Shift+Mod4+F4  exec notify-send -t 800 "R2" "down long"
bindsym Ctrl+Shift+Mod4+F5  exec notify-send -t 800 "R2" "left short"
bindsym Ctrl+Shift+Mod4+F6  exec notify-send -t 800 "R2" "left long"
bindsym Ctrl+Shift+Mod4+F7  exec notify-send -t 800 "R2" "right short"
bindsym Ctrl+Shift+Mod4+F8  exec notify-send -t 800 "R2" "right long"
bindsym Ctrl+Shift+Mod4+F9  exec notify-send -t 800 "R2" "b1 short"
bindsym Ctrl+Shift+Mod4+F10 exec notify-send -t 800 "R2" "b2 long"
```

## Why `Ctrl+Shift+Super+F1..F12`?

The kernel codes `KEY_F13..KEY_F24` are **not** the X `F13..F24` function keys —
in the default evdev keymap they alias `XF86Tools`, `XF86Launch5..`, etc. (one of
them, `F20`, even aliases `XF86AudioMicMute`). So `bindsym F13` never matches.

`F1..F12` have clean keysyms, but they exist on a real keyboard, so we prefix a
distinctive `Ctrl+Shift+Super` combo to avoid collisions.

## Notes

- `learn` rewrites `mapping.yaml` from scratch (it drops the explanatory
  comments), so re-run it only when needed.