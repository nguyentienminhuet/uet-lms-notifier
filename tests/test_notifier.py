"""Unit tests for UET-LMS Notifier v2."""

from __future__ import annotations

from datetime import datetime, timezone
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
        courses: list[dict] | None = None,
        assignments: dict[int, list[dict]] | None = None,
        announcements: dict[int, list[dict]] | None = None,
        discussions: dict[int, list[dict]] | None = None,
        quizzes: dict[int, list[dict]] | None = None,
        modules: dict[int, list[dict]] | None = None,
        files: dict[int, list[dict]] | None = None,
        pages: dict[int, list[dict]] | None = None,
        front_pages: dict[int, dict] | None = None,
        account_notifications: list[dict] | None = None,
        conversations: list[dict] | None = None,
        activity_stream: list[dict] | None = None,
        planner_items: list[dict] | None = None,
        todo_items: list[dict] | None = None,
        events: list[dict] | None = None,
        calendar_events: list[dict] | None = None,
        self_user: dict | None = None,
        enrollments: list[dict] | None = None,
    ) -> None:
        self.base_url = "https://portal.uet.vnu.edu.vn"
        self._courses = courses or []
        self._assignments = assignments or {}
        self._announcements = announcements or {}
        self._discussions = discussions or {}
        self._quizzes = quizzes or {}
        self._modules = modules or {}
        self._files = files or {}
        self._pages = pages or {}
        self._front_pages = front_pages or {}
        self._account_notifications = account_notifications or []
        self._conversations = conversations or []
        self._activity_stream = activity_stream or []
        self._planner_items = planner_items or []
        self._todo_items = todo_items or []
        self._events = events or []
        self._calendar_events = calendar_events or []
        self._self_user = self_user
        self._enrollments = enrollments or []

    def get_active_courses(self) -> list[dict]:
        return self._courses

    def get_course_assignments(self, course_id: int) -> list[dict]:
        return self._assignments.get(course_id, [])

    def get_course_announcements(self, course_id: int) -> list[dict]:
        return self._announcements.get(course_id, [])

    def get_course_discussions(self, course_id: int) -> list[dict]:
        return self._discussions.get(course_id, [])

    def get_course_quizzes(self, course_id: int) -> list[dict]:
        return self._quizzes.get(course_id, [])

    def get_course_modules(self, course_id: int) -> list[dict]:
        return self._modules.get(course_id, [])

    def get_course_files(self, course_id: int) -> list[dict]:
        return self._files.get(course_id, [])

    def get_course_pages(self, course_id: int) -> list[dict]:
        return self._pages.get(course_id, [])

    def get_course_front_page(self, course_id: int) -> dict | None:
        return self._front_pages.get(course_id)

    def get_account_notifications(self) -> list[dict]:
        return self._account_notifications

    def get_conversations(self) -> list[dict]:
        return self._conversations

    def get_activity_stream(self) -> list[dict]:
        return self._activity_stream

    def get_planner_items(self, start_iso: str) -> list[dict]:
        return self._planner_items

    def get_todo_items(self) -> list[dict]:
        return self._todo_items

    def get_upcoming_events(self) -> list[dict]:
        return self._events

    def get_self_user(self) -> dict | None:
        return self._self_user

    def get_calendar_events(self, context_codes: list[str], start_iso: str, end_iso: str) -> list[dict]:
        return [e for e in self._calendar_events if not context_codes or e.get("context_code") in context_codes]

    def get_active_enrollments(self) -> list[dict]:
        return self._enrollments


class NotifierTests(unittest.TestCase):
    NOW = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)

    def test_html_to_text_and_format_vn_time(self) -> None:
        raw = "<p>Nộp <b>Lab 1</b> trước 23:59</p><p>Xem &amp; làm đầy đủ.</p>"
        self.assertEqual(main.html_to_text(raw), "Nộp Lab 1 trước 23:59\nXem & làm đầy đủ.")
        self.assertEqual(main.html_to_text("<p>" + ("a" * 50) + "</p>", max_len=20), ("a" * 17) + "...")
        self.assertEqual(main.format_vn_time("2026-11-20T16:59:00Z"), "23:59 20/11/2026 (UTC+7)")
        self.assertEqual(main.format_vn_time(None), "Không có")

    def test_verifier_tokens_stripped_from_html_hash(self) -> None:
        h1 = '<p>Thông báo</p><a href="/files/1?verifier=abc123">PDF</a>'
        h2 = '<p>Thông báo</p><a href="/files/1?verifier=xyz999">PDF</a>'
        self.assertEqual(main._normalize_html_for_hash(h1), main._normalize_html_for_hash(h2))

    def test_urgent_2h_new_and_existing_and_submitted_assignments(self) -> None:
        # Run 1 (seed baseline): assignment 1 due in 3h (not urgent yet), assignment 2 already submitted & due in 1h
        c1 = FakeCanvasClient(
            courses=[{"id": 101, "name": "AI"}],
            assignments={
                101: [
                    {"id": 1, "name": "BT1", "due_at": "2026-11-17T13:00:00Z", "submission": {"workflow_state": "unsubmitted"}},
                    {"id": 2, "name": "BT2-ĐãNộp", "due_at": "2026-11-17T12:15:00Z", "submission": {"workflow_state": "submitted"}},
                ]
            },
        )
        items0, state1, _ = main.collect_updates(c1, {"initialized": False, "items": {}, "reminded_2h": {}}, now=self.NOW)  # type: ignore[arg-type]
        self.assertEqual(items0, [])

        # Run 2 at 11:30 UTC: BT1 now has 1h30m left (unsubmitted -> urgent!), BT2 submitted -> no alert,
        # BT3 newly created with due in 45m -> immediate assignment_new_urgent!
        later = datetime(2026, 11, 17, 11, 30, 0, tzinfo=timezone.utc)
        c2 = FakeCanvasClient(
            courses=[{"id": 101, "name": "AI"}],
            assignments={
                101: [
                    {"id": 1, "name": "BT1", "due_at": "2026-11-17T13:00:00Z", "submission": {"workflow_state": "unsubmitted"}},
                    {"id": 2, "name": "BT2-ĐãNộp", "due_at": "2026-11-17T12:15:00Z", "submission": {"workflow_state": "submitted"}},
                    {"id": 3, "name": "BT3-MớiGấp", "due_at": "2026-11-17T12:15:00Z", "submission": {"workflow_state": "unsubmitted"}},
                ]
            },
        )
        items1, state2, _ = main.collect_updates(c2, state1, now=later)  # type: ignore[arg-type]
        self.assertEqual([(i.kind, i.title) for i in items1], [("deadline_urgent", "BT1"), ("assignment_new_urgent", "BT3-MớiGấp")])

        # Run 3 at 11:40 UTC: already reminded for BT1 & BT3 -> no duplicate spam!
        items2, _, _ = main.collect_updates(c2, state2, now=datetime(2026, 11, 17, 11, 40, 0, tzinfo=timezone.utc))  # type: ignore[arg-type]
        self.assertEqual(items2, [])

        # Run 4: deadline of BT1 extended to 13:15 UTC (still < 2h away) -> new due_at triggers new reminder!
        c3 = FakeCanvasClient(
            courses=[{"id": 101, "name": "AI"}],
            assignments={
                101: [
                    {"id": 1, "name": "BT1", "due_at": "2026-11-17T13:15:00Z", "submission": {"workflow_state": "unsubmitted"}},
                    {"id": 2, "name": "BT2-ĐãNộp", "due_at": "2026-11-17T12:15:00Z", "submission": {"workflow_state": "submitted"}},
                    {"id": 3, "name": "BT3-MớiGấp", "due_at": "2026-11-17T12:15:00Z", "submission": {"workflow_state": "unsubmitted"}},
                ]
            },
        )
        items3, _, _ = main.collect_updates(c3, state2, now=datetime(2026, 11, 17, 11, 40, 0, tzinfo=timezone.utc))  # type: ignore[arg-type]
        self.assertEqual([(i.kind, i.title) for i in items3], [("deadline_urgent", "BT1")])

    def test_full_course_and_dashboard_coverage_without_duplicates(self) -> None:
        base_client = FakeCanvasClient(
            courses=[{"id": 101, "name": "AI2014", "syllabus_body": "<p>Đề cương v1</p>"}],
            assignments={101: [{"id": 1, "name": "Lab 1", "due_at": "2026-11-20T16:59:00Z", "points_possible": 10, "submission": {"workflow_state": "unsubmitted"}}]},
            announcements={101: [{"id": 10, "title": "TB1", "posted_at": "2026-11-10T00:00:00Z"}]},
            enrollments=[{"course_id": 101, "grades": {"current_score": 0.0}}],
        )
        _, state1, _ = main.collect_updates(base_client, {"initialized": False, "items": {}, "reminded_2h": {}}, now=self.NOW)  # type: ignore[arg-type]

        # Verify state1 stores opaque hashes, NOT plaintext names/grades
        serialized = json.dumps(state1)
        self.assertNotIn("AI2014", serialized)
        self.assertNotIn("Lab 1", serialized)
        self.assertNotIn("Đề cương", serialized)

        updated_client = FakeCanvasClient(
            courses=[{"id": 101, "name": "AI2014", "syllabus_body": "<p>Đề cương v2</p>"}],
            assignments={
                101: [
                    {"id": 1, "name": "Lab 1", "due_at": "2026-11-20T16:59:00Z", "points_possible": 10, "submission": {"workflow_state": "graded", "score": 9.5, "graded_at": "2026-11-17T10:05:00Z"}},
                    {"id": 2, "name": "Lab 2", "due_at": "2026-11-25T16:59:00Z", "submission": {"workflow_state": "unsubmitted"}},
                ]
            },
            announcements={101: [{"id": 10, "title": "TB1", "posted_at": "2026-11-10T00:00:00Z"}]},
            discussions={101: [{"id": 20, "title": "Hỏi đáp BTL", "is_announcement": False, "posted_at": "2026-11-17T10:05:00Z"}]},
            quizzes={
                101: [
                    {"id": 30, "title": "Quiz trùng Lab 2", "assignment_id": 2},
                    {"id": 31, "title": "Quiz thử không chấm điểm", "assignment_id": None, "due_at": "2026-11-22T12:00:00Z"},
                ]
            },
            files={101: [{"id": 40, "display_name": "Slide11.pptx", "updated_at": "2026-11-17T10:05:00Z", "size": 1234}]},
            modules={
                101: [
                    {
                        "id": 50,
                        "name": "Tuần 11",
                        "items": [
                            {"id": 501, "type": "File", "content_id": 40, "title": "Slide11.pptx"},  # deduped because file 40 just emitted
                            {"id": 502, "type": "ExternalUrl", "external_url": "https://example.com", "title": "Tài liệu đọc thêm"},
                        ],
                    }
                ]
            },
            front_pages={101: {"title": "Front Page", "updated_at": "2026-11-17T10:05:00Z", "body": "<p>Xin chào</p>"}},
            pages={101: [{"page_id": 60, "url": "huong-dan", "title": "Hướng dẫn", "updated_at": "2026-11-17T10:05:00Z"}]},
            account_notifications=[{"id": 70, "subject": "Nghỉ lễ", "message": "<p>Thông báo toàn trường</p>"}],
            conversations=[{"id": 80, "subject": "Điểm giữa kỳ", "last_message": "Đã có điểm", "last_message_at": "2026-11-17T10:05:00Z", "message_count": 1}],
            planner_items=[
                {"plannable_type": "assignment", "plannable_id": 2, "plannable": {"title": "Lab 2"}},  # deduped
                {"plannable_type": "planner_note", "plannable_id": 90, "plannable": {"title": "Nhớ ôn thi"}, "plannable_date": "2026-11-19T12:00:00Z"},
            ],
            activity_stream=[
                {"id": 91, "type": "Submission", "assignment_id": 1, "title": "Lab 1", "course_id": 101, "submission_comments": [{"id": 1, "comment": "Làm tốt lắm!", "created_at": "2026-11-17T10:05:00Z"}]},
                {"id": 92, "type": "Message", "title": "Ngày Đến Hạn của Bài Tập Đã Thay Đổi: Lab 1"},  # deduped
            ],
            enrollments=[{"course_id": 101, "grades": {"current_score": 95.0}}],
        )
        items, _, _ = main.collect_updates(updated_client, state1, now=self.NOW)  # type: ignore[arg-type]
        kinds = [i.kind for i in items]
        self.assertIn("grade_updated", kinds)
        self.assertIn("assignment_new", kinds)
        self.assertIn("discussion_new", kinds)
        self.assertIn("quiz_new", kinds)
        self.assertIn("file_new", kinds)
        self.assertIn("module_new", kinds)
        self.assertIn("module_item_new", kinds)
        self.assertIn("page_updated", kinds)
        self.assertIn("page_new", kinds)
        self.assertIn("syllabus_updated", kinds)
        self.assertIn("account_notification_new", kinds)
        self.assertIn("conversation_new", kinds)
        self.assertIn("planner_new", kinds)
        self.assertIn("submission_comment", kinds)
        self.assertIn("course_grade_updated", kinds)

        # Ensure duplicates were filtered cleanly
        titles = [i.title for i in items]
        self.assertNotIn("Quiz trùng Lab 2", titles)
        self.assertNotIn("Tuần 11: Slide11.pptx", titles)
        self.assertNotIn("Ngày Đến Hạn của Bài Tập Đã Thay Đổi: Lab 1", titles)
        self.assertEqual(titles.count("Lab 2"), 1)

    def test_next_link_and_safe_list(self) -> None:
        header = '<https://portal.uet.vnu.edu.vn/api/v1/courses?page=2>; rel="next"'
        self.assertEqual(main.CanvasClient._next_link(header), "https://portal.uet.vnu.edu.vn/api/v1/courses?page=2")
        client = main.CanvasClient("https://portal.uet.vnu.edu.vn", "tok")
        err_404 = HTTPError("https://x", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        with patch.object(client, "get_paginated", side_effect=err_404):
            self.assertEqual(client.get_course_pages(123), [])

    def test_main_init_sends_confirmation_and_immediate_urgent_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            soon = datetime.now(timezone.utc).replace(microsecond=0)
            due_soon = (soon.replace(minute=soon.minute) + main.timedelta(minutes=45)).isoformat().replace("+00:00", "Z")
            fake_client = FakeCanvasClient(
                courses=[{"id": 101, "name": "Giải tích 1"}],
                assignments={101: [{"id": 1, "name": "Quiz gấp", "due_at": due_soon, "submission": {"workflow_state": "unsubmitted"}}]},
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
            self.assertEqual(mock_send.call_count, 2)
            self.assertIn("Kích hoạt", mock_send.call_args_list[0].args[0])
            self.assertIn("[KHẨN - UET-LMS]", mock_send.call_args_list[1].args[0])
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertTrue(saved["initialized"])
            self.assertIn("asg:1", saved["reminded_2h"])

    def test_main_does_not_save_state_if_email_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            fake_client = FakeCanvasClient(
                courses=[{"id": 101, "name": "Giải tích 1"}],
                assignments={101: [{"id": 1, "name": "HW1", "due_at": "2026-11-20T16:59:00Z"}]},
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

    def test_load_state_migrates_v1_schema_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(json.dumps({"initialized": True, "assignments": {"1": "2026-11-20"}}), encoding="utf-8")
            loaded = main.load_state(state_file)
            self.assertFalse(loaded["initialized"])
            self.assertEqual(loaded["items"], {})

    def test_personal_planner_notes_and_calendar_events(self) -> None:
        c0 = FakeCanvasClient(
            courses=[{"id": 101, "name": "AI"}],
            self_user={"id": 32361},
            planner_items=[
                {"plannable_type": "planner_note", "plannable_id": 1, "plannable": {"title": "Ôn tập"}, "plannable_date": "2026-11-17T13:00:00Z", "planner_override": {"marked_complete": False}},
                {"plannable_type": "planner_note", "plannable_id": 2, "plannable": {"title": "Đã xong"}, "plannable_date": "2026-11-17T12:10:00Z", "planner_override": {"marked_complete": True}},
            ],
        )
        _, state1, _ = main.collect_updates(c0, {"initialized": False, "items": {}, "reminded_2h": {}}, now=self.NOW)  # type: ignore[arg-type]

        # At 11:30 UTC: note 1 has 1h30m left -> urgent; note 2 is marked_complete -> skipped;
        # new note 3 due in 40m -> immediate assignment_new_urgent;
        # new personal calendar event 60 days out (user_32361) -> event_new ("Lịch cá nhân") & deduped against planner_items!
        c1 = FakeCanvasClient(
            courses=[{"id": 101, "name": "AI"}],
            self_user={"id": 32361},
            calendar_events=[
                {"id": 888, "type": "event", "title": "Thi cuối kỳ tự đặt", "start_at": "2027-01-15T02:00:00Z", "context_code": "user_32361"},
            ],
            planner_items=[
                {"plannable_type": "planner_note", "plannable_id": 1, "plannable": {"title": "Ôn tập"}, "plannable_date": "2026-11-17T13:00:00Z", "planner_override": {"marked_complete": False}},
                {"plannable_type": "planner_note", "plannable_id": 2, "plannable": {"title": "Đã xong"}, "plannable_date": "2026-11-17T12:10:00Z", "planner_override": {"marked_complete": True}},
                {"plannable_type": "planner_note", "plannable_id": 3, "plannable": {"title": "Ghi chú gấp"}, "plannable_date": "2026-11-17T12:10:00Z", "planner_override": {"marked_complete": False}},
                {"plannable_type": "calendar_event", "plannable_id": 888, "plannable": {"title": "Thi cuối kỳ tự đặt"}, "plannable_date": "2027-01-15T02:00:00Z"},
            ],
        )
        items, _, _ = main.collect_updates(c1, state1, now=datetime(2026, 11, 17, 11, 30, 0, tzinfo=timezone.utc))  # type: ignore[arg-type]
        self.assertEqual(
            [(i.kind, i.title, i.course_name) for i in items],
            [
                ("deadline_urgent", "Ôn tập", "Ghi chú cá nhân (Bảng điều khiển)"),
                ("assignment_new_urgent", "Ghi chú gấp", "Ghi chú cá nhân (Bảng điều khiển)"),
                ("event_new", "Thi cuối kỳ tự đặt", "Lịch cá nhân"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
