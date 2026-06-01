import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


class _DummyRumps(types.SimpleNamespace):
    class App:
        pass

    class Timer:
        pass

    class MenuItem:
        pass


sys.modules.setdefault("rumps", _DummyRumps(separator=object()))

import traffic_light


def _ts(offset_seconds=0):
    return datetime.fromtimestamp(traffic_light._now() + offset_seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def _status(events, mtime=None):
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl") as fp:
        fp.write("\n".join(json.dumps(event) for event in events))
        fp.flush()
        if mtime is not None:
            os.utime(fp.name, (mtime, mtime))
        return traffic_light._session_status(Path(fp.name), {})


class StatusParserTests(unittest.TestCase):
    def base_meta(self):
        return {
            "timestamp": _ts(-5),
            "type": "session_meta",
            "payload": {"id": "s1", "cwd": "/tmp/codex-demo", "model": "gpt-test"},
        }

    def test_new_turn_maps_to_thinking(self):
        status = _status([self.base_meta()])
        self.assertEqual(status.hardware_state, "thinking")
        self.assertEqual(status.hardware_mode, "green")
        self.assertEqual(status.state, "green")

    def test_assistant_response_maps_to_marquee(self):
        status = _status(
            [
                self.base_meta(),
                {"timestamp": _ts(-4), "type": "event_msg", "payload": {"type": "user_message"}},
                {
                    "timestamp": _ts(-3),
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "phase": "commentary"},
                },
            ]
        )
        self.assertEqual(status.hardware_state, "generating")
        self.assertEqual(status.hardware_mode, "thinking")
        self.assertEqual(status.state, "green")

    def test_running_tool_maps_to_marquee(self):
        status = _status(
            [
                self.base_meta(),
                {"timestamp": _ts(-4), "type": "event_msg", "payload": {"type": "user_message"}},
                {
                    "timestamp": _ts(-3),
                    "type": "response_item",
                    "payload": {"type": "function_call", "call_id": "c1", "arguments": json.dumps({"cmd": "make test"})},
                },
            ]
        )
        self.assertEqual(status.hardware_state, "generating")
        self.assertEqual(status.hardware_mode, "thinking")
        self.assertEqual(status.state, "green")

    def test_waiting_permission_maps_to_review_request(self):
        status = _status(
            [
                self.base_meta(),
                {
                    "timestamp": _ts(-4),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "c1",
                        "arguments": json.dumps({"sandbox_permissions": "require_escalated"}),
                    },
                },
            ]
        )
        self.assertEqual(status.hardware_state, "review_request")
        self.assertEqual(status.hardware_mode, "busy")
        self.assertEqual(status.state, "yellow")

    def test_success_final_maps_to_success(self):
        status = _status(
            [
                self.base_meta(),
                {"timestamp": _ts(-3), "type": "event_msg", "payload": {"type": "user_message"}},
                {
                    "timestamp": _ts(-2),
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "phase": "final"},
                },
            ]
        )
        self.assertEqual(status.hardware_state, "success")
        self.assertEqual(status.hardware_mode, "red_blink_5")
        self.assertEqual(status.state, "red")

    def test_error_final_maps_to_error(self):
        status = _status(
            [
                self.base_meta(),
                {"timestamp": _ts(-5), "type": "event_msg", "payload": {"type": "user_message"}},
                {
                    "timestamp": _ts(-4),
                    "type": "response_item",
                    "payload": {"type": "function_call_output", "call_id": "c1", "output": "Process exited with code 1"},
                },
                {
                    "timestamp": _ts(-3),
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "phase": "final"},
                },
            ]
        )
        self.assertEqual(status.hardware_state, "error")
        self.assertEqual(status.hardware_mode, "error")
        self.assertEqual(status.state, "red")

    def test_stale_session_maps_to_idle(self):
        stale_time = traffic_light._now() - traffic_light.ACTIVE_GRACE_SECONDS - 5
        status = _status(
            [
                {
                    "timestamp": datetime.fromtimestamp(stale_time, timezone.utc).isoformat().replace("+00:00", "Z"),
                    "type": "session_meta",
                    "payload": {"id": "s1", "cwd": "/tmp/codex-demo"},
                }
            ],
            mtime=stale_time,
        )
        self.assertEqual(status.hardware_state, "idle")
        self.assertEqual(status.hardware_mode, "traffic")
        self.assertEqual(status.state, "red")


if __name__ == "__main__":
    unittest.main()
