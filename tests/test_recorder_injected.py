# -*- coding: utf-8 -*-
"""Recorder hotkey-injection behavior: injected relative deltas become 「转向」
actions, injected absolute moves are not recorded, physical input is unchanged.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from macroflow.input.recorder import MacroRecorder  # noqa: E402

from pynput import mouse  # noqa: E402


class FakeRawListener:
    def __init__(self, cb):
        self.cb = cb

    def start(self):
        pass

    def stop(self):
        pass


def make_recorder(mode="auto", interval_ms=100):
    rec = MacroRecorder()
    rec.mode = mode
    rec.interval_ms = interval_ms
    rec.target_relative_enabled = False
    rec.running = True
    rec._last_action_time = time.perf_counter()
    return rec


class RecorderInjectedInputTests(unittest.TestCase):
    def test_injected_relative_delta_records_turn(self):
        rec = make_recorder()
        rec._on_raw_move(-1500, 0, injected=True)
        rec._flush_injected(force=True)
        self.assertEqual(len(rec.actions), 1)
        action = rec.actions[0]
        self.assertEqual(action["type"], "turn")
        self.assertEqual(action["dx"], -1500)
        self.assertEqual(action["dy"], 0)

    def test_injected_burst_merges_into_one_turn(self):
        rec = make_recorder()
        rec._on_raw_move(-500, 0, injected=True)
        rec._on_raw_move(-500, 0, injected=True)
        rec._on_raw_move(-500, 0, injected=True)
        rec._flush_injected(force=True)
        self.assertEqual(len(rec.actions), 1)
        self.assertEqual(rec.actions[0]["dx"], -1500)

    def test_physical_relative_delta_stays_mouse_move(self):
        rec = make_recorder(mode="relative")
        rec._on_raw_move(-300, 0, injected=False)
        rec._flush_raw(force=True)
        self.assertEqual(len(rec.actions), 1)
        self.assertEqual(rec.actions[0]["type"], "mouse_move")
        self.assertEqual(rec.actions[0]["mode"], "relative")
        self.assertEqual(rec.actions[0]["dx"], -300)

    def test_injected_absolute_move_not_recorded(self):
        rec = make_recorder(mode="absolute")
        rec._on_move(100, 200, injected=True)
        self.assertEqual(len(rec.actions), 0)
        rec._on_move(300, 400, injected=False)
        self.assertEqual(len(rec.actions), 1)
        self.assertEqual(rec.actions[0]["type"], "mouse_move")
        self.assertEqual(rec.actions[0]["mode"], "absolute")
        self.assertEqual(rec.actions[0]["x"], 300)

    def test_stop_flushes_remaining_injected_turn(self):
        rec = make_recorder()
        rec._on_raw_move(0, 1500, injected=True)
        actions = rec.stop()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["type"], "turn")
        self.assertEqual(actions[0]["dy"], 1500)

    def test_injected_turn_has_delay_ms(self):
        rec = make_recorder()
        rec._on_raw_move(-900, 0, injected=True)
        rec._flush_injected(force=True)
        self.assertIn("delay_ms", rec.actions[0])
        self.assertGreaterEqual(rec.actions[0]["delay_ms"], 0)

    def test_injected_turn_delay_measured_from_injection_not_flush(self):
        """注入转向的延时从快捷键按下时刻起算，而不是从刷出时刻起算。"""
        rec = make_recorder()
        started = time.perf_counter()
        rec._last_action_time = started - 5.0  # 上一次动作在 5 秒前
        rec._on_raw_move(-1500, 0, injected=True)
        time.sleep(0.15)  # 超过注入脉冲窗口后才刷出
        rec._flush_injected(force=True)
        self.assertEqual(len(rec.actions), 1)
        delay = rec.actions[0]["delay_ms"]
        # 延时应接近 5000ms（按下时刻起算），而不是接近 5150ms（刷出时刻起算）。
        self.assertGreaterEqual(delay, 4900)
        self.assertLessEqual(delay, 5100)

    def test_physical_event_after_injection_flushes_turn_before_it(self):
        """注入转向后紧跟实体点击：转向应排在点击之前，且延时从按下时刻起算。"""
        rec = make_recorder()
        rec._last_action_time = time.perf_counter() - 2.0
        rec._on_raw_move(-1500, 0, injected=True)
        time.sleep(0.15)
        rec._on_click(100, 200, mouse.Button.left, True)
        self.assertEqual(len(rec.actions), 2)
        self.assertEqual(rec.actions[0]["type"], "turn")
        self.assertEqual(rec.actions[1]["type"], "mouse_button")
        # 转向延时按按下时刻（2 秒前）起算；点击延时从转向按下时刻起算。
        self.assertGreaterEqual(rec.actions[0]["delay_ms"], 1900)
        self.assertLessEqual(rec.actions[0]["delay_ms"], 2100)
        self.assertLess(rec.actions[1]["delay_ms"], 1000)

    def test_two_separate_hotkey_presses_keep_own_delays(self):
        """两次间隔较长的快捷键注入：各自延时按各自按下时刻起算，不串扰。"""
        rec = make_recorder()
        rec._last_action_time = time.perf_counter() - 10.0
        rec._on_raw_move(-1500, 0, injected=True)
        time.sleep(0.15)
        rec._on_raw_move(1500, 0, injected=True)
        time.sleep(0.15)
        rec._flush_injected(force=True)
        self.assertEqual(len(rec.actions), 2)
        # 第一次注入延时 ≈ 10s；第二次注入延时 = 第一次按下后到第二次按下 ≈ 0.15s。
        first = rec.actions[0]["delay_ms"]
        second = rec.actions[1]["delay_ms"]
        self.assertGreaterEqual(first, 9900)
        self.assertLessEqual(first, 10100)
        self.assertGreaterEqual(second, 100)
        self.assertLessEqual(second, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
