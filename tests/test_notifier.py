"""Unit tests for UET-LMS Notifier."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import main


class FakeCanvasClient:
    def __init__(
        self,
        courses: list[dict],
        assignments: dict[int, list[dict]],
        announcements: dict[int, list[dict]],
        events: list[dict] | None = None,
    ) -> None:
        self.base_url = "https://portal.uet.vnu.edu.vn"
        self._courses = courses
        self._assignments = assignments
        self._announcements = announcements
        self._events = events or []

    def get_active_courses(self) -> list[dict]:
        return self._courses

    def get_course_assignments(self, course_id: int) -> list[dict]:
        return self._assignments.get(course_id, [])

    def get_course_announcements(self, course_id: int) -> list[dict]:
        return self._announcements.get(course_id, [])

    def get_upcoming_events(self) -> list[dict]:
        return self._events


class NotifierTests(unittest.TestCase):
    def test_html_to_text_strips_and_truncates(self) -> None:
        raw = "<p>Nộp <b>Lab 1</b> trước 23:59</p><p>Xem &amp; làm đầy đủ.</p>"
        self.assertEqual(main.html_to_text(raw), "Nộp Lab 1 trước 23:59\nXem & làm đầy đủ.")
        long_text = "<p>" + ("a" * 50) + "</p>"
        self.assertEqual(main.html_to_text(long_text, max_len=20), ("a" * 17) + "...")

    def test_format_vn_time_converts_utc_to_utc7(self) -> None:
        self.assertEqual(main.format_vn_time("2026-11-20T16:59:00Z"), "23:59 20/11/2026 (UTC+7)")
        self.assertEqual(main.format_vn_time(None), "Không có")

    def test_collect_updates_detects_new_items_and_deadline_changes(self) -> None:
        client = FakeCanvasClient(
            courses=[{"id": 101, "name": "INT2204 - Lập trình hướng đối tượng"}],
            assignments={
                101: [
                    {"id": 1, "name": "Lab 1", "due_at": "2026-11-20T16:59:00Z", "published": True},
                    {"id": 2, "name": "Lab 2", "due_at": "2026-11-25T16:59:00Z", "published": True},
                    {"id": 3, "name": "Draft", "due_at": None, "published": False},
                ]
            },
            announcements={
                101: [
                    {"id": 10, "title": "Nghỉ học thứ 5", "posted_at": "2026-11-17T02:00:00Z", "message": "<p>Chú ý</p>"}
                ]
            },
            events=[
                {"id": 900, "type": "event", "title": "Thi giữa kỳ", "start_at": "2026-11-30T01:00:00Z", "context_code": "course_101"},
                {"id": 901, "type": "assignment", "title": "Lab 2"},
            ],
        )
        prev_state = {
            "initialized": True,
            "assignments": {"1": "2026-11-19T16:59:00Z"},
            "announcements": {},
            "events": {},
        }

        items, next_state, _ = main.collect_updates(client, prev_state)  # type: ignore[arg-type]
        kinds = [(i.kind, i.title) for i in items]
        self.assertEqual(
            kinds,
            [
                ("assignment_due_changed", "Lab 1"),
                ("assignment_new", "Lab 2"),
                ("announcement", "Nghỉ học thứ 5"),
                ("event_new", "Thi giữa kỳ"),
            ],
        )
        self.assertTrue(next_state["initialized"])
        self.assertEqual(next_state["assignments"]["1"], "2026-11-20T16:59:00Z")
        self.assertNotIn("3", next_state["assignments"])
        self.assertNotIn("901", next_state["events"])

    def test_next_link_parses_canvas_pagination_header(self) -> None:
        header = (
            '<https://portal.uet.vnu.edu.vn/api/v1/courses?page=2>; rel="next", '
            '<https://portal.uet.vnu.edu.vn/api/v1/courses?page=5>; rel="last"'
        )
        self.assertEqual(
            main.CanvasClient._next_link(header),
            "https://portal.uet.vnu.edu.vn/api/v1/courses?page=2",
        )
        self.assertIsNone(main.CanvasClient._next_link('<https://x>; rel="last"'))

    def test_safe_course_list_skips_403_and_404(self) -> None:
        client = main.CanvasClient("https://portal.uet.vnu.edu.vn", "tok")
        err_404 = HTTPError("https://x", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        with patch.object(client, "get_paginated", side_effect=err_404):
            self.assertEqual(client.get_course_assignments(123), [])

        err_401 = HTTPError("https://x", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
        with patch.object(client, "get_paginated", side_effect=err_401):
            with self.assertRaises(HTTPError):
                client.get_course_assignments(123)

    def test_main_first_run_seeds_state_and_sends_single_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            fake_client = FakeCanvasClient(
                courses=[{"id": 101, "name": "Giải tích 1"}],
                assignments={101: [{"id": 1, "name": "HW1", "due_at": "2026-11-20T16:59:00Z"}]},
                announcements={101: [{"id": 10, "title": "Chào mừng", "posted_at": "2026-11-01T00:00:00Z"}]},
            )
            env = {
                "CANVAS_TOKEN": "fake-token",
                "SMTP_USER": "sender@gmail.com",
                "SMTP_PASS": "app-pass",
                "TO_EMAIL": "25022948@vnu.edu.vn",
                "STATE_FILE": str(state_file),
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(main, "CanvasClient", return_value=fake_client),
                patch.object(main, "send_email") as mock_send,
            ):
                rc = main.main()

            self.assertEqual(rc, 0)
            mock_send.assert_called_once()
            self.assertIn("Kích hoạt", mock_send.call_args.args[0])
            self.assertEqual(mock_send.call_args.args[7], "25022948@vnu.edu.vn")
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertTrue(saved["initialized"])
            self.assertEqual(saved["assignments"], {"1": "2026-11-20T16:59:00Z"})

    def test_main_does_not_save_state_if_email_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            fake_client = FakeCanvasClient(
                courses=[{"id": 101, "name": "Giải tích 1"}],
                assignments={101: [{"id": 1, "name": "HW1", "due_at": "2026-11-20T16:59:00Z"}]},
                announcements={},
            )
            env = {
                "CANVAS_TOKEN": "fake-token",
                "SMTP_USER": "sender@gmail.com",
                "SMTP_PASS": "bad-pass",
                "TO_EMAIL": "25022948@vnu.edu.vn",
                "STATE_FILE": str(state_file),
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(main, "CanvasClient", return_value=fake_client),
                patch.object(main, "send_email", side_effect=OSError("auth failed")),
            ):
                rc = main.main()

            self.assertEqual(rc, 1)
            self.assertFalse(state_file.exists())


if __name__ == "__main__":
    unittest.main()
