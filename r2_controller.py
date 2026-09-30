#!/usr/bin/env python3
"""R2 ring controller.

Grabs the R2 Bluetooth ring's input nodes (touch + consumer control) so the
host never sees them as a mouse/touch/scroll source, then decodes the synthetic
touch gestures / consumer keys and re-emits them as keyboard keys through a
virtual uinput device.

Modes:
    python3 r2_controller.py learn    # interactive calibration -> mapping.yaml
    python3 r2_controller.py run      # remap daemon
    python3 r2_controller.py capture  # raw event dump (debug)
"""

import argparse
import select
import sys
import time
from pathlib import Path

from evdev import InputDevice, UInput, ecodes, list_devices

try:
    import yaml
except ImportError:
    yaml = None


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEVICE_MATCH = "R2"
VIRT_KB_NAME = "PSKeyboard"
DEFAULT_CONFIG_PATH = Path(__file__).parent / "mapping.yaml"

# A contact with less travel than this (device units) is a tap.
TAP_TRAVEL = 20.0

# travel/duration (units per second). Short swipes are ~7000-15000 u/s, long
# presses drift at ~800 u/s, and b1's ultra-fast "flick" is ~34000 u/s.
SHORT_SPEED = 3000.0
FLICK_SPEED = 20000.0

# A long press may be split by the ring into several contacts. Wait this long
# after the last slow contact before firing the long action.
LONG_QUIET_MS = 300

DEFAULT_BUTTONS = ["up", "down", "left", "right", "b1", "b2"]

# Keys emitted for each learned signal, in order. The F13..F24 kernel codes
# alias XF86 media keys in X, so we instead emit a distinctive modifier combo:
# Ctrl+Shift+Super+F1..F12 (sway: bindsym Ctrl+Shift+Mod4+F1 ...).
COMBO_PREFIX = "ctrl+shift+super+"
DEFAULT_KEY_SEQUENCE = [f"{COMBO_PREFIX}F{n}" for n in range(1, 13)]

MODIFIER_ALIASES = {
    "super": "KEY_LEFTMETA", "mod4": "KEY_LEFTMETA", "win": "KEY_LEFTMETA",
    "meta": "KEY_LEFTMETA",
    "ctrl": "KEY_LEFTCTRL", "control": "KEY_LEFTCTRL",
    "alt": "KEY_LEFTALT",
    "shift": "KEY_LEFTSHIFT", "lshift": "KEY_LEFTSHIFT",
    "rshift": "KEY_RIGHTSHIFT",
}

# sway keysym names for our modifier aliases.
_SWAY_MOD = {
    "ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt",
    "shift": "Shift", "lshift": "Shift", "rshift": "Shift",
    "super": "Mod4", "mod4": "Mod4", "win": "Mod4", "meta": "Mod4",
}


def sway_combo(key):
    """Translate a binding key/combine ('ctrl+shift+super+F1') to sway syntax
    ('Ctrl+Shift+Mod4+F1')."""
    parts = []
    for p in str(key).split("+"):
        p = p.strip()
        if not p:
            continue
        norm = p.lower().replace("_", "")
        if norm in _SWAY_MOD:
            parts.append(_SWAY_MOD[norm])
        elif p.upper().startswith("KEY_"):
            parts.append(p.upper()[4:])
        else:
            parts.append(p.upper())
    return "+".join(parts)


# ---------------------------------------------------------------------------
# Key name resolution
# ---------------------------------------------------------------------------


def resolve_key(name):
    """Resolve 'F13', 'KEY_F13', 'KEY_PLAYPAUSE' -> kernel keycode int."""
    if isinstance(name, int):
        return name
    raw = name.strip()
    norm = raw.lower().replace("_", "").replace(" ", "")
    if norm in MODIFIER_ALIASES:
        return getattr(ecodes, MODIFIER_ALIASES[norm])
    for cand in (raw.upper(), "KEY_" + raw.upper()):
        if hasattr(ecodes, cand):
            val = getattr(ecodes, cand)
            if isinstance(val, int):
                return val
    if len(raw) == 1 and raw.isalpha():
        return ecodes.KEY_A + ord(raw.lower()) - ord("a")
    if len(raw) == 1 and raw.isdigit():
        return ecodes.KEY_0 + int(raw)
    raise ValueError(f"unknown key {name!r}")


# ---------------------------------------------------------------------------
# Discovery / grab
# ---------------------------------------------------------------------------


def find_r2_devices():
    """Return list of (node_name, InputDevice) for R2 nodes, grabbed."""
    devices = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if dev.name == VIRT_KB_NAME:
            continue
        if DEVICE_MATCH in dev.name:
            try:
                dev.grab()
            except OSError:
                pass
            devices.append((dev.name, dev))
    return devices


def open_keyboards():
    """Open non-R2 devices that look like keyboards (have KEY_ENTER), not grabbed."""
    kbs = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if DEVICE_MATCH in dev.name:
            continue
        caps = dev.capabilities()
        if ecodes.EV_KEY in caps and ecodes.KEY_ENTER in caps[ecodes.EV_KEY]:
            kbs.append(dev)
    return kbs


# ---------------------------------------------------------------------------
# Gesture classification
# ---------------------------------------------------------------------------


class GestureTracker:
    """Accumulates single-touch contacts and classifies them by speed.

    The R2 reports contact via ABS_MT_TRACKING_ID (>=0 down, -1 up) and only
    moves one axis per gesture (X for left/right, Y for up/down). A missing
    axis is treated as zero delta.
    """

    def __init__(self, short_speed=SHORT_SPEED, tap_travel=TAP_TRAVEL,
                 flick_speed=FLICK_SPEED):
        self.short_speed = short_speed
        self.tap_travel = tap_travel
        self.flick_speed = flick_speed
        self.active = False
        self.start = None
        self.end = None
        self.start_time = None

    def feed(self, ev):
        """Feed one event; return a classification dict on contact end, else None."""
        if ev.type != ecodes.EV_ABS:
            return None

        if ev.code == ecodes.ABS_MT_TRACKING_ID:
            if ev.value >= 0:
                self.active = True
                self.start = None
                self.end = None
                self.start_time = time.monotonic()
            else:
                result = self._finish()
                self.active = False
                self.start = None
                self.end = None
                self.start_time = None
                return result
        elif self.active:
            if ev.code == ecodes.ABS_MT_POSITION_X:
                _, y = self.end if self.end else (None, None)
                self.end = (ev.value, y)
                if self.start is None:
                    self.start = self.end
            elif ev.code == ecodes.ABS_MT_POSITION_Y:
                x, _ = self.end if self.end else (None, None)
                self.end = (x, ev.value)
                if self.start is None:
                    self.start = self.end
        return None

    def _finish(self):
        duration = time.monotonic() - self.start_time if self.start_time else 0.0
        if self.start is None or self.end is None:
            return {"kind": "tap", "dx": 0, "dy": 0, "travel": 0.0,
                    "duration": duration, "speed": 0.0}

        sx, sy = self.start
        ex, ey = self.end
        dx = (ex or 0) - (sx or 0)
        dy = (ey or 0) - (sy or 0)
        travel = (dx * dx + dy * dy) ** 0.5
        speed = travel / duration if duration > 1e-6 else 0.0
        base = {"dx": dx, "dy": dy, "travel": travel,
                "duration": duration, "speed": speed}

        if travel < self.tap_travel:
            base["kind"] = "tap"
            return base

        if abs(dx) >= abs(dy):
            direction = "right" if dx > 0 else "left"
        else:
            direction = "down" if dy > 0 else "up"
        base["kind"] = "swipe"
        base["direction"] = direction
        if speed >= self.flick_speed:
            base["press"] = "flick"
        elif speed >= self.short_speed:
            base["press"] = "short"
        else:
            base["press"] = "long"
        return base


# ---------------------------------------------------------------------------
# uinput emitter
# ---------------------------------------------------------------------------


class Emitter:
    """Virtual keyboard that emits key combos."""

    def __init__(self):
        keys = set()
        for n in range(1, 25):
            keys.add(getattr(ecodes, f"KEY_F{n}"))
        for name in ("KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_ENTER",
                     "KEY_ESC", "KEY_SPACE", "KEY_PLAYPAUSE", "KEY_NEXTSONG",
                     "KEY_PREVIOUSSONG", "KEY_STOP", "KEY_VOLUMEUP",
                     "KEY_VOLUMEDOWN", "KEY_MUTE", "KEY_MICMUTE",
                     "KEY_LEFTCTRL", "KEY_RIGHTCTRL", "KEY_LEFTSHIFT",
                     "KEY_RIGHTSHIFT", "KEY_LEFTALT", "KEY_RIGHTALT",
                     "KEY_LEFTMETA", "KEY_RIGHTMETA"):
            if hasattr(ecodes, name):
                keys.add(getattr(ecodes, name))
        self.dev = UInput({ecodes.EV_KEY: sorted(keys)}, name=VIRT_KB_NAME)

    def _keycodes(self, key):
        if isinstance(key, int):
            return [key]
        return [resolve_key(part) for part in str(key).split("+") if part.strip()]

    def emit(self, key):
        if key is None:
            return
        try:
            codes = self._keycodes(key)
        except (ValueError, AttributeError) as exc:
            print(f"bad binding {key!r}: {exc}", file=sys.stderr)
            return
        for c in codes:
            self.dev.write(ecodes.EV_KEY, c, 1)
        for c in reversed(codes):
            self.dev.write(ecodes.EV_KEY, c, 0)
        self.dev.syn()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_mapping(path):
    """Return dict with tunables and 'bindings' list."""
    config = {"tap_travel": TAP_TRAVEL, "short_speed": SHORT_SPEED,
              "flick_speed": FLICK_SPEED, "long_quiet_ms": LONG_QUIET_MS,
              "bindings": []}
    if yaml is not None and Path(path).exists():
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        for k in ("tap_travel", "short_speed", "flick_speed", "long_quiet_ms"):
            if k in data:
                config[k] = data[k]
        config["bindings"] = data.get("bindings", []) or []
    return config


def lookup(bindings, node, kind, direction, press, source_key):
    for b in bindings:
        if b.get("node") and b.get("node") != node:
            continue
        if b.get("kind") and b.get("kind") != kind:
            continue
        if b.get("direction") and b.get("direction") != direction:
            continue
        if b.get("press") and b.get("press") != press:
            continue
        if b.get("source") and b.get("source") != source_key:
            continue
        return b.get("key")
    return None


# ---------------------------------------------------------------------------
# Shared event reading
# ---------------------------------------------------------------------------


def _gesture_label(r):
    if r["kind"] == "tap":
        return "tap"
    return f"{r['kind']} {r['direction']} {r['press']}"


_INVERT_DIR = {"up": "down", "down": "up", "left": "right", "right": "left"}


def _gesture_detail(r):
    """Human-readable gesture, with the direction shown as the physical button
    (the device reports it inverted, natural-scroll style)."""
    if r["kind"] == "tap":
        return f"tap (travel {r['travel']:.0f}u, {r['duration']:.3f}s)"
    if r["press"] == "flick":
        label = "flick"
    else:
        label = f"{_INVERT_DIR.get(r['direction'], r['direction'])} {r['press']}"
    return (f"{label} ({r['travel']:.0f}u, "
            f"{r['duration']:.3f}s, {r['speed']:.0f}u/s)")


# ---------------------------------------------------------------------------
# Mode: capture
# ---------------------------------------------------------------------------


def run_capture(args):
    devices = find_r2_devices()
    if not devices:
        print("no R2 device found; is the ring connected?", file=sys.stderr)
        return 1
    fds = {dev.fd: (name, dev) for name, dev in devices}
    trackers = {dev.fd: GestureTracker() for _, dev in devices}
    print("capture mode: press buttons, ctrl-c to quit", file=sys.stderr)

    while True:
        r, _, _ = select.select(list(fds), [], [])
        for fd in r:
            name, dev = fds[fd]
            try:
                events = dev.read()
            except OSError:
                continue
            for ev in events:
                if ev.type == ecodes.EV_SYN:
                    continue
                if ev.type == ecodes.EV_ABS:
                    result = trackers[fd].feed(ev)
                    if result:
                        print(f"[{name}] {result}")
                elif ev.type == ecodes.EV_KEY and ev.value == 1:
                    if ev.code == ecodes.BTN_TOUCH:
                        continue
                    keyname = ecodes.KEY.get(ev.code, str(ev.code))
                    print(f"[{name}] key {keyname} pressed")


# ---------------------------------------------------------------------------
# Mode: learn
# ---------------------------------------------------------------------------


class QuitCalibration(Exception):
    pass


def _acquire_signal(r2_devices, kb_devs, first_timeout=8.0, settle=0.8):
    """Wait for one ring press. Returns ('skip'|'timeout'|'signal', payload)."""
    r2_fds = {dev.fd: (name, dev) for name, dev in r2_devices}
    kb_fds = {dev.fd: dev for dev in kb_devs}
    trackers = {dev.fd: GestureTracker() for _, dev in r2_devices}
    all_fds = list(r2_fds) + list(kb_fds)

    gestures = []
    keys = []
    t_start = time.monotonic()
    first_at = None
    last_r2_at = None

    while True:
        now = time.monotonic()
        if first_at is None:
            if now - t_start > first_timeout:
                return ("timeout", None)
            timeout = first_timeout - (now - t_start)
        else:
            if now - last_r2_at > settle:
                break
            timeout = settle - (now - last_r2_at)

        r, _, _ = select.select(all_fds, [], [], max(0.0, min(timeout, 0.2)))

        for fd in r:
            if fd in kb_fds:
                dev = kb_fds[fd]
                try:
                    events = dev.read()
                except OSError:
                    continue
                for ev in events:
                    if ev.type == ecodes.EV_KEY and ev.value == 1:
                        if ev.code == ecodes.KEY_ESC:
                            raise QuitCalibration()
                        if ev.code == ecodes.KEY_ENTER:
                            return ("skip", None)
                continue

            name, dev = r2_fds[fd]
            try:
                events = dev.read()
            except OSError:
                continue
            for ev in events:
                if ev.type == ecodes.EV_SYN:
                    continue
                if ev.type == ecodes.EV_ABS:
                    result = trackers[fd].feed(ev)
                    if result:
                        gestures.append(result)
                        last_r2_at = time.monotonic()
                        first_at = first_at or last_r2_at
                elif ev.type == ecodes.EV_KEY:
                    if ev.code == ecodes.BTN_TOUCH:
                        continue
                    if ev.value == 1:
                        keys.append(ecodes.KEY.get(ev.code, str(ev.code)))
                        last_r2_at = time.monotonic()
                        first_at = first_at or last_r2_at

    return ("signal", {"gestures": gestures, "keys": keys})


def _summarize(payload):
    """Reduce captured gestures/keys to unique signal descriptors."""
    uniq_g = []
    seen_g = set()
    for g in payload["gestures"]:
        key = (g["kind"], g.get("direction"), g.get("press"))
        if key not in seen_g:
            seen_g.add(key)
            uniq_g.append(g)
    uniq_k = []
    for k in payload["keys"]:
        if k not in uniq_k:
            uniq_k.append(k)
    return uniq_g, uniq_k


def _binding_for_gesture(g, target):
    b = {"node": "R2", "kind": g["kind"], "key": target}
    if g["kind"] == "swipe":
        b["direction"] = g["direction"]
        b["press"] = g["press"]
    return b


def _binding_for_key(name, target):
    return {"node": "R2 Consumer Control", "source": name, "key": target}


def _gesture_signal_key(g):
    """Identity of a learned touch signal (for dedupe)."""
    return ("gesture", g["kind"], g.get("direction"), g.get("press"))


def _key_signal_key(name):
    """Identity of a learned consumer key (for dedupe)."""
    return ("key", name)


def run_learn(args):
    if yaml is None:
        print("PyYAML required for learn mode", file=sys.stderr)
        return 1

    devices = find_r2_devices()
    if not devices:
        print("no R2 device found; is the ring connected?", file=sys.stderr)
        return 1

    print("=== R2 calibration ===")
    print("Buttons:", ", ".join(DEFAULT_BUTTONS))
    print("Press Enter on your real keyboard to SKIP, Esc to QUIT.\n")

    key_seq = list(DEFAULT_KEY_SEQUENCE)
    key_idx = 0
    bindings = []
    report = []
    seen = {}  # signal-key -> target key (for dedupe)
    keyboards = open_keyboards()

    try:
        for button in DEFAULT_BUTTONS:
            for press in ("short", "long"):
                prompt = f"[{button:>5}] {press:<5} press..."
                sys.stdout.write(prompt + "\r")
                sys.stdout.flush()
                status, payload = _acquire_signal(devices, keyboards)

                if status == "skip":
                    report.append((button, press, "skipped", None))
                    print(f"{prompt} skipped")
                    continue
                if status == "timeout":
                    report.append((button, press, "none", None))
                    print(f"{prompt} none (timeout)")
                    continue

                uniq_g, uniq_k = _summarize(payload)
                if not uniq_g and not uniq_k:
                    report.append((button, press, "none", None))
                    print(f"{prompt} none")
                    continue

                descs = [_gesture_detail(g) for g in uniq_g] + [f"key {k}" for k in uniq_k]

                # Split into signals that are new vs already learned (dedupe).
                new_signals = []   # (obj, signal_key, is_gesture)
                dup_targets = []
                for g in uniq_g:
                    sk = _gesture_signal_key(g)
                    (dup_targets.append(seen[sk]) if sk in seen
                     else new_signals.append((g, sk, True)))
                for k in uniq_k:
                    sk = _key_signal_key(k)
                    (dup_targets.append(seen[sk]) if sk in seen
                     else new_signals.append((k, sk, False)))

                if not new_signals:
                    shares = ", ".join(sorted(set(dup_targets)))
                    report.append((button, press, f"same as {shares}", None))
                    print(f"{prompt} {' -> '.join(descs)}  (same signal as {shares})")
                    continue

                if key_idx >= len(key_seq):
                    print(f"{prompt} no target key left, skipping", file=sys.stderr)
                    report.append((button, press, "no-target", None))
                    continue
                target = key_seq[key_idx]
                key_idx += 1

                for obj, sk, is_g in new_signals:
                    if is_g:
                        bindings.append(_binding_for_gesture(obj, target))
                        if obj.get("press") == "flick":
                            # A flick is just a fast tap; add a tap fallback.
                            tsk = ("gesture", "tap", None, None)
                            if tsk not in seen:
                                bindings.append({"node": "R2", "kind": "tap",
                                                 "key": target})
                                seen[tsk] = target
                    else:
                        bindings.append(_binding_for_key(obj, target))
                    seen[sk] = target

                suffix = ""
                if dup_targets:
                    suffix = f"  (also shares {', '.join(sorted(set(dup_targets)))})"
                report.append((button, press, " -> ".join(descs), target))
                print(f"{prompt} {report[-1][2]}  => {target}{suffix}")

    except QuitCalibration:
        print("\nquitting calibration")
        return 1
    finally:
        for kb in keyboards:
            try:
                kb.close()
            except OSError:
                pass

    config = {
        "tap_travel": TAP_TRAVEL,
        "short_speed": SHORT_SPEED,
        "flick_speed": FLICK_SPEED,
        "long_quiet_ms": LONG_QUIET_MS,
        "bindings": bindings,
    }
    out_path = Path(args.config)
    with open(out_path, "w") as fh:
        yaml.safe_dump(config, fh, sort_keys=False, default_flow_style=False)

    print(f"\nwrote {out_path}")
    print("\nLearned mapping:")
    for button, press, desc, target in report:
        tgt = f"  => {target}" if target else ""
        print(f"  {button:>5} {press:<5} {desc}{tgt}")
    if bindings:
        print("\nSway example (test bindings; replace notify-send with your commands):")
        for button, press, desc, target in report:
            if target:
                print(f'  bindsym {sway_combo(target)} exec notify-send -t 800 "R2" "{button} {press}"')
    return 0


# ---------------------------------------------------------------------------
# Mode: run
# ---------------------------------------------------------------------------


def run_daemon(args):
    config = load_mapping(args.config)
    tracker = GestureTracker(config["short_speed"], config["tap_travel"],
                             config["flick_speed"])
    emitter = Emitter()
    quiet = config["long_quiet_ms"] / 1000.0

    pending_long = None  # {"direction": str, "key": str}
    last_activity = 0.0
    devices = []

    def fire(key):
        if key:
            emitter.emit(key)

    def rescan():
        nonlocal devices
        for _, dev in devices:
            try:
                dev.close()
            except OSError:
                pass
        devices = find_r2_devices()
        if devices:
            print(f"running: grabbed {[n for n, _ in devices]}", file=sys.stderr)
        return {dev.fd: (name, dev) for name, dev in devices}

    fds = rescan()
    if not fds:
        print("no R2 device found; waiting for the ring...", file=sys.stderr)

    while True:
        if not fds:
            time.sleep(1.0)
            fds = rescan()
            if not fds:
                continue
            print("R2 device connected", file=sys.stderr)

        now = time.monotonic()
        if pending_long:
            timeout = max(0.0, quiet - (now - last_activity))
        else:
            timeout = 1.0
        timeout = max(0.0, min(timeout, 0.2))
        r, _, _ = select.select(list(fds), [], [], timeout)

        died = False
        for fd in r:
            entry = fds.get(fd)
            if entry is None:
                continue
            name, dev = entry
            try:
                events = dev.read()
            except OSError:
                fds.pop(fd, None)
                try:
                    dev.close()
                except OSError:
                    pass
                died = True
                continue
            for ev in events:
                if ev.type == ecodes.EV_SYN:
                    continue
                last_activity = time.monotonic()
                if ev.type == ecodes.EV_ABS:
                    result = tracker.feed(ev)
                    if not result:
                        continue
                    direction = result.get("direction")
                    press = result.get("press")
                    key = lookup(config["bindings"], name, result["kind"],
                                 direction, press, None)
                    if result["kind"] == "swipe" and press == "long":
                        pending_long = {"direction": direction, "key": key}
                    else:
                        pending_long = None
                        fire(key)
                elif ev.type == ecodes.EV_KEY and ev.value == 1:
                    if ev.code == ecodes.BTN_TOUCH:
                        continue
                    keyname = ecodes.KEY.get(ev.code, str(ev.code))
                    pending_long = None
                    fire(lookup(config["bindings"], name, "key", None, None, keyname))

        now = time.monotonic()
        if pending_long and (now - last_activity) >= quiet:
            fire(pending_long.get("key"))
            pending_long = None

        if died:
            fds = rescan()
            if not fds:
                print("R2 device disconnected; waiting...", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="R2 ring controller")
    parser.add_argument("mode", choices=["learn", "run", "capture"])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args()

    if args.mode == "learn":
        sys.exit(run_learn(args))
    if args.mode == "capture":
        sys.exit(run_capture(args))
    sys.exit(run_daemon(args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass