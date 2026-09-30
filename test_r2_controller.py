"""Regression tests for the R2 gesture classifier.

Run with: python3 -m unittest test_r2_controller -v
"""

import time
import unittest

from evdev import ecodes

import r2_controller as rc


class Ev:
    def __init__(self, type_, code, value):
        self.type = type_
        self.code = code
        self.value = value


def feed_gesture(xs=None, ys=None, duration=0.1):
    """Feed one contact. xs/ys are lists of ABS_MT_POSITION_X/Y samples."""
    t = rc.GestureTracker()
    t.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, 1))
    if xs:
        for v in xs:
            t.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_X, v))
    if ys:
        for v in ys:
            t.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_Y, v))
    t.start_time = time.monotonic() - duration
    return t.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, -1))


def feed_two(first, second):
    """Feed two gestures on the same tracker (returns the second result)."""
    t = rc.GestureTracker()
    for xs, ys, dur in (first, second):
        t.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, 1))
        for v in xs:
            t.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_X, v))
        for v in ys:
            t.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_POSITION_Y, v))
        t.start_time = time.monotonic() - dur
        result = t.feed(Ev(ecodes.EV_ABS, ecodes.ABS_MT_TRACKING_ID, -1))
    return result


class GestureTests(unittest.TestCase):
    def test_up_button_is_device_down(self):
        r = feed_gesture(ys=[557, 629, 845, 1364], duration=0.1)
        self.assertEqual((r["kind"], r["direction"], r["press"]),
                         ("swipe", "down", "short"))

    def test_down_button_is_device_up(self):
        r = feed_gesture(ys=[1481, 1049, 682], duration=0.1)
        self.assertEqual((r["kind"], r["direction"], r["press"]),
                         ("swipe", "up", "short"))

    def test_right_button_is_device_left(self):
        r = feed_gesture(xs=[1429, 829, 20], duration=0.1)
        self.assertEqual((r["kind"], r["direction"], r["press"]),
                         ("swipe", "left", "short"))

    def test_left_button_is_device_right(self):
        r = feed_gesture(xs=[609, 1022, 2027], duration=0.105)
        self.assertEqual((r["kind"], r["direction"], r["press"]),
                         ("swipe", "right", "short"))

    def test_tap(self):
        r = feed_gesture(ys=[1022], duration=0.03)
        self.assertEqual(r["kind"], "tap")

    def test_tap_with_no_samples(self):
        r = feed_gesture(duration=0.03)
        self.assertEqual(r["kind"], "tap")

    def test_flick_down(self):
        r = feed_gesture(ys=[0, 1022], duration=0.03)
        self.assertEqual((r["kind"], r["direction"], r["press"]),
                         ("swipe", "down", "flick"))

    def test_long_drift(self):
        r = feed_gesture(ys=[1121, 1209, 1305], duration=0.4)
        self.assertEqual(r["press"], "long")

    def test_down_after_horizontal_keeps_direction(self):
        """Regression: a stray X sample at the start of a vertical gesture must
        not turn 'down' into 'up'."""
        r = feed_two(
            ([1429, 20], [], 0.1),          # right short (horizontal)
            ([20], [1481, 1049, 682], 0.1),  # down short (vertical, stray X)
        )
        self.assertEqual((r["kind"], r["direction"], r["press"]),
                         ("swipe", "up", "short"))

    def test_up_after_horizontal_keeps_direction(self):
        r = feed_two(
            ([1429, 20], [], 0.1),
            ([20], [557, 845, 1364], 0.1),
        )
        self.assertEqual(r["direction"], "down")


class SuppressorTests(unittest.TestCase):
    def test_first_press_allowed(self):
        s = rc.RepeatSuppressor(400)
        self.assertTrue(s.allow("KEY_VOLUMEUP", now=0.0))

    def test_repeat_within_window_suppressed(self):
        s = rc.RepeatSuppressor(400)
        self.assertTrue(s.allow("KEY_VOLUMEUP", now=0.0))
        self.assertFalse(s.allow("KEY_VOLUMEUP", now=0.285))
        self.assertFalse(s.allow("KEY_VOLUMEUP", now=0.570))

    def test_after_window_allowed(self):
        s = rc.RepeatSuppressor(400)
        self.assertTrue(s.allow("KEY_VOLUMEUP", now=0.0))
        self.assertTrue(s.allow("KEY_VOLUMEUP", now=0.5))

    def test_different_sources_independent(self):
        s = rc.RepeatSuppressor(400)
        self.assertTrue(s.allow("KEY_VOLUMEUP", now=0.0))
        self.assertTrue(s.allow("KEY_VOLUMEDOWN", now=0.1))


class MappingTests(unittest.TestCase):
    def test_hold_repeat_config(self):
        cfg = rc.load_mapping("mapping.yaml")
        self.assertEqual(cfg["hold_repeat_ms"], 400)

    def test_all_targets_resolve(self):
        cfg = rc.load_mapping("mapping.yaml")
        for binding in cfg["bindings"]:
            for part in binding["key"].split("+"):
                rc.resolve_key(part)

    def test_modifier_aliases(self):
        self.assertEqual(rc.resolve_key("super"), ecodes.KEY_LEFTMETA)
        self.assertEqual(rc.resolve_key("shift"), ecodes.KEY_LEFTSHIFT)
        self.assertEqual(rc.resolve_key("ctrl"), ecodes.KEY_LEFTCTRL)

    def test_sway_combo(self):
        self.assertEqual(rc.sway_combo("ctrl+shift+super+F1"),
                         "Ctrl+Shift+Mod4+F1")


if __name__ == "__main__":
    unittest.main()
