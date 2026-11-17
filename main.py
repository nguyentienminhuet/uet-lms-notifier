"""UET-LMS (Canvas) Notifier: full-coverage Canvas monitor & email alerter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import hashlib
from html import escape, unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import smtplib
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

VN_TZ = timezone(timedelta(hours=7))
DEFAULT_BASE_URL = "https://portal.uet.vnu.edu.vn"
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587
REQUEST_TIMEOUT_SEC = 20
URGENT_WINDOW_SEC = 2 * 3600

KIND_LABELS = {
    "assignment_new_urgent": "KHẨN - Bài tập MỚI (< 2h)",
    "deadline_urgent": "KHẨN - Sắp hết hạn (< 2h)",
    "assignment_new": "Bài tập mới",
    "assignment_due_changed": "Cập nhật Deadline",
    "assignment_updated": "Cập nhật Bài tập",
    "grade_updated": "Điểm mới / Cập nhật điểm",
    "course_grade_updated": "Cập nhật Điểm tổng kết môn",
    "submission_comment": "Phản hồi / Nhận xét mới",
    "announcement_new": "Thông báo chung mới",
    "announcement_updated": "Cập nhật Thông báo chung",
    "discussion_new": "Thảo luận mới",
    "discussion_updated": "Bình luận / Cập nhật Thảo luận",
    "quiz_new": "Bài kiểm tra (Quiz) mới",
    "quiz_updated": "Cập nhật Bài kiểm tra",
    "module_new": "Học phần mới (Trang chủ)",
    "module_item_new": "Mục mới trên Trang chủ / Học phần",
    "module_item_updated": "Cập nhật mục trên Trang chủ",
    "file_new": "Tập tin mới",
    "file_updated": "Cập nhật Tập tin",
    "page_new": "Trang (Page) mới",
    "page_updated": "Cập nhật Trang / Trang chủ",
    "syllabus_updated": "Cập nhật Chương trình học",
    "account_notification_new": "Thông báo toàn hệ thống UET-LMS",
    "account_notification_updated": "Cập nhật Thông báo toàn hệ thống",
    "conversation_new": "Tin nhắn Hộp thư đến mới",
    "conversation_updated": "Tin nhắn Hộp thư đến có phản hồi",
    "planner_new": "Mục mới trên Bảng điều khiển",
    "planner_updated": "Cập nhật trên Bảng điều khiển",
    "activity_new": "Hoạt động mới (Bảng điều khiển)",
    "todo_new": "Mục Cần Làm mới",
    "event_new": "Sự kiện / Lịch mới",
    "event_changed": "Cập nhật Lịch / Sự kiện",
}

URGENT_KINDS = {"assignment_new_urgent", "deadline_urgent"}


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML-to-text converter for Canvas descriptions."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "tr"}:
            self._parts.append("\n")

    def get_text(self) -> str:
        raw = unescape("".join(self._parts))
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def html_to_text(raw_html: str | None, max_len: int = 350) -> str:
    """Strip HTML tags and truncate cleanly for email summaries."""
    if not raw_html:
        return ""
    parser = _HTMLTextExtractor()
    parser.feed(raw_html)
    parser.close()
    text = re.sub(r"[ \t]+", " ", parser.get_text()).strip()
    if max_len > 0 and len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text


def _normalize_html_for_hash(raw_html: str | None) -> str:
    """Strip ephemeral Canvas verifier tokens so hash only tracks real edits."""
    if not raw_html:
        return ""
    cleaned = re.sub(r"verifier=[A-Za-z0-9_-]+", "", raw_html)
    return html_to_text(cleaned, max_len=0)


def digest(*parts: Any) -> str:
    """Short opaque SHA-256 hex digest (safe to store in a public repo)."""
    payload = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def parse_iso_dt(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def format_vn_time(iso_str: str | None) -> str:
    """Convert Canvas ISO-8601 UTC timestamp to readable UTC+7 string."""
    if not iso_str:
        return "Không có"
    dt = parse_iso_dt(iso_str)
    if not dt:
        return iso_str
    return dt.astimezone(VN_TZ).strftime("%H:%M %d/%m/%Y (UTC+7)")


def format_remaining(seconds: float) -> str:
    total_mins = max(1, int(round(seconds / 60)))
    hours, mins = divmod(total_mins, 60)
    if hours and mins:
        return f"còn {hours} giờ {mins} phút"
    if hours:
        return f"còn {hours} giờ"
    return f"còn {mins} phút"


@dataclass(frozen=True)
class NotificationItem:
    kind: str
    course_name: str
    title: str
    url: str
    due_at: str | None = None
    summary: str = ""
    extra_line: str = ""

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, "Cập nhật LMS")

    @property
    def is_urgent(self) -> bool:
        return self.kind in URGENT_KINDS


class CanvasClient:
    """Lightweight Canvas LMS REST API client using stdlib urllib."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request_json(self, url: str) -> tuple[Any, dict[str, str]]:
        req = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "uet-lms-notifier/2.0",
            },
        )
        with urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            data = json.loads(resp.read().decode("utf-8"))
            return data, headers

    @staticmethod
    def _next_link(link_header: str | None) -> str | None:
        if not link_header:
            return None
        for part in link_header.split(","):
            if 'rel="next"' in part:
                match = re.search(r"<([^>]+)>", part)
                if match:
                    return match.group(1)
        return None

    def get_paginated(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query = urlencode(params or {}, doseq=True)
        url: str | None = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        results: list[dict[str, Any]] = []
        while url:
            data, headers = self._request_json(url)
            if isinstance(data, list):
                results.extend(data)
            else:
                break
            url = self._next_link(headers.get("link"))
        return results

    def _safe_list(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        try:
            return self.get_paginated(path, params)
        except HTTPError as err:
            if err.code in {403, 404}:
                return []
            raise

    def _safe_dict(self, path: str) -> dict[str, Any] | None:
        try:
            data, _ = self._request_json(f"{self.base_url}{path}")
            return data if isinstance(data, dict) else None
        except HTTPError as err:
            if err.code in {403, 404}:
                return None
            raise

    def get_active_courses(self) -> list[dict[str, Any]]:
        courses = self.get_paginated(
            "/api/v1/courses",
            {"enrollment_state": "active", "include[]": "syllabus_body", "per_page": 100},
        )
        return [c for c in courses if c.get("id") and c.get("name")]

    def get_course_assignments(self, course_id: int) -> list[dict[str, Any]]:
        return self._safe_list(
            f"/api/v1/courses/{course_id}/assignments",
            {"include[]": "submission", "per_page": 100},
        )

    def get_course_announcements(self, course_id: int) -> list[dict[str, Any]]:
        return self._safe_list(
            f"/api/v1/courses/{course_id}/discussion_topics",
            {"only_announcements": "true", "per_page": 50},
        )

    def get_course_discussions(self, course_id: int) -> list[dict[str, Any]]:
        return self._safe_list(
            f"/api/v1/courses/{course_id}/discussion_topics",
            {"per_page": 50},
        )

    def get_course_quizzes(self, course_id: int) -> list[dict[str, Any]]:
        return self._safe_list(f"/api/v1/courses/{course_id}/quizzes", {"per_page": 100})

    def get_course_modules(self, course_id: int) -> list[dict[str, Any]]:
        return self._safe_list(
            f"/api/v1/courses/{course_id}/modules",
            {"include[]": "items", "per_page": 100},
        )

    def get_course_files(self, course_id: int) -> list[dict[str, Any]]:
        return self._safe_list(f"/api/v1/courses/{course_id}/files", {"per_page": 100})

    def get_course_pages(self, course_id: int) -> list[dict[str, Any]]:
        return self._safe_list(f"/api/v1/courses/{course_id}/pages", {"per_page": 100})

    def get_course_front_page(self, course_id: int) -> dict[str, Any] | None:
        return self._safe_dict(f"/api/v1/courses/{course_id}/front_page")

    def get_account_notifications(self) -> list[dict[str, Any]]:
        return self._safe_list("/api/v1/accounts/self/users/self/account_notifications")

    def get_conversations(self) -> list[dict[str, Any]]:
        return self._safe_list("/api/v1/conversations", {"per_page": 30})

    def get_activity_stream(self) -> list[dict[str, Any]]:
        return self._safe_list("/api/v1/users/self/activity_stream", {"per_page": 40})

    def get_planner_items(self, start_iso: str) -> list[dict[str, Any]]:
        return self._safe_list(
            "/api/v1/planner/items",
            {"start_date": start_iso, "per_page": 100},
        )

    def get_todo_items(self) -> list[dict[str, Any]]:
        return self._safe_list("/api/v1/users/self/todo")

    def get_upcoming_events(self) -> list[dict[str, Any]]:
        return self._safe_list("/api/v1/users/self/upcoming_events")

    def get_active_enrollments(self) -> list[dict[str, Any]]:
        return self._safe_list("/api/v1/users/self/enrollments", {"state[]": "active"})


class _StateTracker:
    """Tracks opaque SHA-256 signatures and 2h reminders."""

    def __init__(self, prev_state: dict[str, Any], now: datetime) -> None:
        self.initialized = bool(prev_state.get("initialized", False))
        self.prev_items: dict[str, str] = dict(prev_state.get("items", {}))
        self.prev_reminded: dict[str, str] = dict(prev_state.get("reminded_2h", {}))
        self.next_items: dict[str, str] = dict(self.prev_items)
        self.next_reminded: dict[str, str] = dict(self.prev_reminded)
        self.now = now
        self.notifications: list[NotificationItem] = []
        self.emitted_refs: set[tuple[str, str]] = set()
        self.seen_assignment_ids: set[str] = set()
        self.seen_announcement_ids: set[str] = set()
        self.seen_discussion_ids: set[str] = set()
        self.seen_quiz_ids: set[str] = set()

    def track(self, key: str, sig: str) -> str | None:
        self.next_items[key] = sig
        if not self.initialized:
            return None
        prev = self.prev_items.get(key)
        if prev is None:
            return "new"
        if prev != sig:
            return "changed"
        return None

    def check_urgent_2h(
        self,
        rem_key: str,
        due_at: str | None,
        course_name: str,
        title: str,
        url: str,
        is_new_item: bool = False,
        submitted: bool = False,
        summary: str = "",
    ) -> bool:
        """Emit urgent <2h notification once per (item, due_at); return True if emitted."""
        if submitted or not due_at:
            return False
        due_dt = parse_iso_dt(due_at)
        if not due_dt:
            return False
        remaining = (due_dt - self.now).total_seconds()
        if not (0 < remaining <= URGENT_WINDOW_SEC):
            return False

        due_sig = digest(due_at)
        if self.prev_reminded.get(rem_key) == due_sig:
            return False

        self.next_reminded[rem_key] = due_sig
        kind = "assignment_new_urgent" if (is_new_item and self.initialized) else "deadline_urgent"
        self.notifications.append(
            NotificationItem(
                kind=kind,
                course_name=course_name,
                title=title,
                url=url,
                due_at=due_at,
                summary=summary,
                extra_line=f"Khẩn: {format_remaining(remaining)} trước hạn nộp!",
            )
        )
        return True

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "initialized": True,
            "items": self.next_items,
            "reminded_2h": self.next_reminded,
        }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"initialized": False, "items": {}, "reminded_2h": {}}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    items = dict(data.get("items", {}))
    # If migrating from v1 state.json (which had no 'items' hash map), re-seed cleanly without flooding old items.
    initialized = bool(data.get("initialized", False)) and bool(items)
    return {
        "initialized": initialized,
        "items": items,
        "reminded_2h": dict(data.get("reminded_2h", {})),
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _resolve_context_name(context_code: str, course_names: dict[int, str]) -> str:
    if context_code.startswith("course_"):
        try:
            cid = int(context_code.split("_", 1)[1])
            return course_names.get(cid, context_code)
        except ValueError:
            pass
    return context_code or "UET-LMS"


def _collect_course_assignments(client: CanvasClient, tracker: _StateTracker, cid: int, cname: str) -> None:
    for item in client.get_course_assignments(cid):
        if item.get("published") is False:
            continue
        aid = str(item["id"])
        tracker.seen_assignment_ids.add(aid)
        due_at = item.get("due_at")
        title = item.get("name") or f"Assignment {aid}"
        url = item.get("html_url") or f"{client.base_url}/courses/{cid}/assignments/{aid}"
        summary = html_to_text(item.get("description"))

        sub = item.get("submission") or {}
        submitted = sub.get("workflow_state") in {"submitted", "graded"} or bool(sub.get("submitted_at"))

        due_status = tracker.track(f"asg:due:{aid}", digest(due_at))
        meta_status = tracker.track(
            f"asg:meta:{aid}",
            digest(title, _normalize_html_for_hash(item.get("description")), item.get("points_possible"), item.get("lock_at")),
        )

        is_new = due_status == "new"
        if is_new:
            tracker.emitted_refs.add(("Assignment", aid))
        urgent_emitted = tracker.check_urgent_2h(
            f"asg:{aid}", due_at, cname, title, url, is_new_item=is_new, submitted=submitted, summary=summary
        )

        if is_new and not urgent_emitted:
            tracker.notifications.append(
                NotificationItem("assignment_new", cname, title, url, due_at=due_at, summary=summary)
            )
        elif due_status == "changed" and not urgent_emitted:
            tracker.notifications.append(
                NotificationItem(
                    "assignment_due_changed",
                    cname,
                    title,
                    url,
                    due_at=due_at,
                    summary=summary,
                    extra_line=f"Deadline đã thay đổi -> Mới: {format_vn_time(due_at)}",
                )
            )
        elif meta_status == "changed" and due_status is None:
            tracker.notifications.append(
                NotificationItem("assignment_updated", cname, title, url, due_at=due_at, summary=summary)
            )

        has_grade = any(sub.get(k) is not None for k in ("graded_at", "score", "grade"))
        grade_sig = digest(sub.get("graded_at"), sub.get("score"), sub.get("grade"), sub.get("entered_score"), sub.get("excused"))
        grade_status = tracker.track(f"asg:grade:{aid}", grade_sig)
        if grade_status in {"new", "changed"} and has_grade:
            score_val = sub.get("score") if sub.get("score") is not None else sub.get("grade")
            pts = item.get("points_possible")
            score_txt = f"{score_val}/{pts}" if pts is not None else str(score_val)
            tracker.notifications.append(
                NotificationItem(
                    "grade_updated",
                    cname,
                    title,
                    url,
                    due_at=sub.get("graded_at"),
                    extra_line=f"Điểm mới: {score_txt}",
                )
            )


def _collect_course_topics(client: CanvasClient, tracker: _StateTracker, cid: int, cname: str) -> None:
    for topic in client.get_course_announcements(cid):
        if topic.get("published") is False:
            continue
        tid = str(topic["id"])
        tracker.seen_announcement_ids.add(tid)
        title = topic.get("title") or f"Announcement {tid}"
        url = topic.get("html_url") or f"{client.base_url}/courses/{cid}/announcements/{tid}"
        summary = html_to_text(topic.get("message"))
        sig = digest(
            title,
            _normalize_html_for_hash(topic.get("message")),
            topic.get("posted_at"),
            topic.get("discussion_subentry_count"),
            topic.get("last_reply_at"),
        )
        status = tracker.track(f"ann:{tid}", sig)
        if status == "new":
            tracker.emitted_refs.add(("Announcement", tid))
            tracker.notifications.append(
                NotificationItem("announcement_new", cname, title, url, due_at=topic.get("posted_at"), summary=summary)
            )
        elif status == "changed":
            tracker.notifications.append(
                NotificationItem("announcement_updated", cname, title, url, due_at=topic.get("last_reply_at") or topic.get("posted_at"), summary=summary)
            )

    for topic in client.get_course_discussions(cid):
        if topic.get("published") is False or topic.get("is_announcement"):
            continue
        tid = str(topic["id"])
        if tid in tracker.seen_announcement_ids:
            continue
        tracker.seen_discussion_ids.add(tid)
        title = topic.get("title") or f"Discussion {tid}"
        url = topic.get("html_url") or f"{client.base_url}/courses/{cid}/discussion_topics/{tid}"
        summary = html_to_text(topic.get("message"))
        sig = digest(
            title,
            _normalize_html_for_hash(topic.get("message")),
            topic.get("discussion_subentry_count"),
            topic.get("last_reply_at"),
        )
        status = tracker.track(f"disc:{tid}", sig)
        if status == "new":
            tracker.emitted_refs.add(("Discussion", tid))
            tracker.notifications.append(
                NotificationItem("discussion_new", cname, title, url, due_at=topic.get("posted_at"), summary=summary)
            )
        elif status == "changed":
            tracker.notifications.append(
                NotificationItem("discussion_updated", cname, title, url, due_at=topic.get("last_reply_at"), summary=summary)
            )


def _collect_course_quizzes(client: CanvasClient, tracker: _StateTracker, cid: int, cname: str) -> None:
    for quiz in client.get_course_quizzes(cid):
        if quiz.get("published") is False:
            continue
        qid = str(quiz["id"])
        tracker.seen_quiz_ids.add(qid)
        asg_id = quiz.get("assignment_id")
        if asg_id is not None and str(asg_id) in tracker.seen_assignment_ids:
            continue

        due_at = quiz.get("due_at")
        title = quiz.get("title") or f"Quiz {qid}"
        url = quiz.get("html_url") or f"{client.base_url}/courses/{cid}/quizzes/{qid}"
        summary = html_to_text(quiz.get("description"))
        sig = digest(title, due_at, quiz.get("lock_at"), quiz.get("question_count"), _normalize_html_for_hash(quiz.get("description")))
        status = tracker.track(f"quiz:{qid}", sig)
        is_new = status == "new"
        if is_new:
            tracker.emitted_refs.add(("Quiz", qid))
        urgent_emitted = tracker.check_urgent_2h(
            f"quiz:{qid}", due_at, cname, title, url, is_new_item=is_new, summary=summary
        )
        if is_new and not urgent_emitted:
            tracker.notifications.append(NotificationItem("quiz_new", cname, title, url, due_at=due_at, summary=summary))
        elif status == "changed" and not urgent_emitted:
            tracker.notifications.append(NotificationItem("quiz_updated", cname, title, url, due_at=due_at, summary=summary))


def _collect_course_content(client: CanvasClient, tracker: _StateTracker, course: dict[str, Any]) -> None:
    cid = course["id"]
    cname = course["name"]

    # Syllabus (Chương trình học)
    syllabus = course.get("syllabus_body")
    if syllabus:
        syl_status = tracker.track(f"syllabus:{cid}", digest(_normalize_html_for_hash(syllabus)))
        if syl_status in {"new", "changed"}:
            tracker.notifications.append(
                NotificationItem(
                    "syllabus_updated",
                    cname,
                    "Chương trình học",
                    f"{client.base_url}/courses/{cid}/assignments/syllabus",
                    summary=html_to_text(syllabus),
                )
            )

    # Front page (Trang chủ nếu môn dùng Wiki Page làm trang chủ)
    front = client.get_course_front_page(cid)
    if front and front.get("published") is not False:
        fp_sig = digest(front.get("title"), front.get("updated_at"), _normalize_html_for_hash(front.get("body")))
        fp_status = tracker.track(f"front:{cid}", fp_sig)
        if fp_status in {"new", "changed"}:
            tracker.notifications.append(
                NotificationItem(
                    "page_updated",
                    cname,
                    front.get("title") or "Trang chủ môn học",
                    front.get("html_url") or f"{client.base_url}/courses/{cid}",
                    due_at=front.get("updated_at"),
                    summary=html_to_text(front.get("body")),
                )
            )

    # Pages (Trang)
    for page in client.get_course_pages(cid):
        if page.get("published") is False or page.get("front_page"):
            continue
        slug = str(page.get("url") or page.get("page_id"))
        pid = str(page.get("page_id") or slug)
        title = page.get("title") or slug
        url = page.get("html_url") or f"{client.base_url}/courses/{cid}/pages/{slug}"
        status = tracker.track(f"page:{cid}:{pid}", digest(title, page.get("updated_at")))
        if status == "new":
            tracker.emitted_refs.add(("Page", slug))
            tracker.notifications.append(NotificationItem("page_new", cname, title, url, due_at=page.get("updated_at")))
        elif status == "changed":
            tracker.notifications.append(NotificationItem("page_updated", cname, title, url, due_at=page.get("updated_at")))

    # Files (Tập Tin)
    for fobj in client.get_course_files(cid):
        fid = str(fobj["id"])
        name = fobj.get("display_name") or fobj.get("filename") or f"File {fid}"
        url = fobj.get("url") or f"{client.base_url}/courses/{cid}/files/{fid}"
        status = tracker.track(f"file:{fid}", digest(name, fobj.get("updated_at"), fobj.get("size")))
        if status == "new":
            tracker.emitted_refs.add(("File", fid))
            tracker.notifications.append(NotificationItem("file_new", cname, name, url, due_at=fobj.get("updated_at")))
        elif status == "changed":
            tracker.notifications.append(NotificationItem("file_updated", cname, name, url, due_at=fobj.get("updated_at")))

    # Modules (Trang chủ & Các học phần: Tuần 1, Slide, Học liệu...)
    for mod in client.get_course_modules(cid):
        if mod.get("published") is False:
            continue
        mid = str(mod["id"])
        mname = mod.get("name") or f"Module {mid}"
        mod_url = f"{client.base_url}/courses/{cid}/modules"
        if tracker.track(f"mod:{mid}", digest(mname)) == "new":
            tracker.notifications.append(NotificationItem("module_new", cname, mname, mod_url))

        for item in mod.get("items") or []:
            if item.get("published") is False:
                continue
            iid = str(item["id"])
            mtype = str(item.get("type") or "")
            content_id = item.get("content_id")
            page_url = item.get("page_url")
            ref_val = str(page_url if mtype == "Page" and page_url else content_id or "")
            title = item.get("title") or f"Item {iid}"
            url = item.get("html_url") or item.get("external_url") or mod_url
            status = tracker.track(f"moditem:{iid}", digest(title, mtype, item.get("external_url"), content_id))
            if status is None:
                continue
            if status == "new" and (mtype, ref_val) in tracker.emitted_refs:
                continue
            kind = "module_item_new" if status == "new" else "module_item_updated"
            tracker.notifications.append(
                NotificationItem(kind, cname, f"{mname}: {title}", url, extra_line=f"Loại: {mtype or 'Mục học phần'}")
            )


def _collect_global_and_dashboard(
    client: CanvasClient,
    tracker: _StateTracker,
    course_names: dict[int, str],
) -> None:
    # 1. Global banners (Thông báo toàn UET-LMS)
    for notif in client.get_account_notifications():
        nid = str(notif["id"])
        title = notif.get("subject") or f"Thông báo hệ thống {nid}"
        summary = html_to_text(notif.get("message"))
        sig = digest(title, _normalize_html_for_hash(notif.get("message")), notif.get("start_at"), notif.get("end_at"))
        status = tracker.track(f"acct:{nid}", sig)
        if status == "new":
            tracker.notifications.append(
                NotificationItem("account_notification_new", "UET-LMS Toàn hệ thống", title, client.base_url, due_at=notif.get("start_at"), summary=summary)
            )
        elif status == "changed":
            tracker.notifications.append(
                NotificationItem("account_notification_updated", "UET-LMS Toàn hệ thống", title, client.base_url, due_at=notif.get("start_at"), summary=summary)
            )

    # 2. Inbox / Conversations (Hộp thư đến)
    for conv in client.get_conversations():
        cid = str(conv["id"])
        title = conv.get("subject") or "(Không có tiêu đề)"
        url = f"{client.base_url}/conversations#filter=type=inbox&conversation_id={cid}"
        summary = html_to_text(conv.get("last_message"))
        status = tracker.track(f"conv:{cid}", digest(title, conv.get("last_message_at"), conv.get("message_count")))
        if status == "new":
            tracker.notifications.append(
                NotificationItem("conversation_new", "Hộp thư đến (Inbox)", title, url, due_at=conv.get("last_message_at"), summary=summary)
            )
        elif status == "changed":
            tracker.notifications.append(
                NotificationItem("conversation_updated", "Hộp thư đến (Inbox)", title, url, due_at=conv.get("last_message_at"), summary=summary)
            )

    # 3. Course total scores (Enrollments)
    for enr in client.get_active_enrollments():
        cid = enr.get("course_id")
        if not cid:
            continue
        grades = enr.get("grades") or {}
        has_any = any(grades.get(k) is not None for k in ("current_score", "final_score", "current_grade", "final_grade"))
        sig = digest(grades.get("current_score"), grades.get("final_score"), grades.get("current_grade"), grades.get("final_grade"))
        status = tracker.track(f"enr_grade:{cid}", sig)
        if status in {"new", "changed"} and has_any:
            cname = course_names.get(cid, f"Course {cid}")
            score_val = next(
                (grades.get(k) for k in ("current_score", "final_score", "current_grade", "final_grade") if grades.get(k) is not None),
                None,
            )
            score_fmt = f"{score_val}%" if isinstance(score_val, (int, float)) else str(score_val)
            tracker.notifications.append(
                NotificationItem(
                    "course_grade_updated",
                    cname,
                    "Điểm tổng kết hiện tại",
                    grades.get("html_url") or f"{client.base_url}/courses/{cid}/grades",
                    extra_line=f"Điểm hiện tại: {score_fmt}",
                )
            )

    # 4. Dashboard Planner items (Ghi chú & các mục Bảng điều khiển chưa theo dõi trực tiếp)
    start_iso = (tracker.now - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
    for pitem in client.get_planner_items(start_iso):
        ptype = str(pitem.get("plannable_type") or "")
        pid = str(pitem.get("plannable_id") or "")
        if not pid:
            continue
        if ptype == "assignment" and pid in tracker.seen_assignment_ids:
            continue
        if ptype == "announcement" and pid in tracker.seen_announcement_ids:
            continue
        if ptype == "discussion_topic" and pid in tracker.seen_discussion_ids:
            continue
        if ptype == "quiz" and pid in tracker.seen_quiz_ids:
            continue
        pl = pitem.get("plannable") or {}
        title = pl.get("title") or pl.get("name") or f"{ptype} {pid}"
        date_str = pitem.get("plannable_date") or pl.get("due_at") or pl.get("todo_date")
        cname = pitem.get("context_name") or _resolve_context_name(str(pitem.get("context_type") or ""), course_names)
        raw_url = pitem.get("html_url") or f"{client.base_url}/"
        url = raw_url if str(raw_url).startswith("http") else f"{client.base_url}{raw_url}"
        status = tracker.track(f"plan:{ptype}:{pid}", digest(title, date_str, pl.get("details")))
        if status == "new":
            tracker.notifications.append(NotificationItem("planner_new", cname, title, url, due_at=date_str, summary=html_to_text(pl.get("details"))))
        elif status == "changed":
            tracker.notifications.append(NotificationItem("planner_updated", cname, title, url, due_at=date_str, summary=html_to_text(pl.get("details"))))

    # 5. Dashboard Activity Stream (Nhận xét bài chấm, Hội thảo, Cộng tác, Thông báo hệ thống)
    for act in client.get_activity_stream():
        aid = str(act.get("id") or "")
        atype = str(act.get("type") or "")
        if not aid or atype in {"Conversation", "Announcement", "DiscussionTopic"}:
            continue
        cname = course_names.get(act.get("course_id"), "Bảng Điều Khiển")
        url = act.get("html_url") or f"{client.base_url}/"

        if atype == "Submission":
            comments = act.get("submission_comments") or []
            if not comments:
                continue
            last_comment = comments[-1]
            sig = digest(len(comments), last_comment.get("id"), last_comment.get("created_at"))
            status = tracker.track(f"act_sub_cmt:{act.get('assignment_id') or aid}", sig)
            if status in {"new", "changed"}:
                tracker.notifications.append(
                    NotificationItem(
                        "submission_comment",
                        cname,
                        act.get("title") or "Bài nộp",
                        url,
                        due_at=last_comment.get("created_at"),
                        summary=html_to_text(last_comment.get("comment")),
                    )
                )
            continue

        title = act.get("title") or atype
        if atype == "Message" and ("Ngày Đến Hạn" in title or "Due Date" in title):
            continue
        status = tracker.track(f"act:{atype}:{aid}", digest(title, act.get("updated_at")))
        if status == "new":
            tracker.notifications.append(
                NotificationItem("activity_new", cname, title, url, due_at=act.get("created_at"), summary=html_to_text(act.get("message")))
            )

    # 6. Sidebar To-Do fallback (Cần Làm)
    for td in client.get_todo_items():
        asg = td.get("assignment") or {}
        asg_id = asg.get("id")
        if asg_id is not None and str(asg_id) in tracker.seen_assignment_ids:
            continue
        td_key = str(td.get("ignore") or asg_id or "")
        if not td_key:
            continue
        title = asg.get("name") or (td.get("quiz") or {}).get("title") or "Mục Cần Làm"
        cname = td.get("context_name") or "UET-LMS"
        url = asg.get("html_url") or f"{client.base_url}/"
        if tracker.track(f"todo:{digest(td_key)}", digest(title, asg.get("due_at"))) == "new":
            tracker.notifications.append(NotificationItem("todo_new", cname, title, url, due_at=asg.get("due_at")))

    # 7. Upcoming Calendar Events (Lịch)
    for ev in client.get_upcoming_events():
        if ev.get("type") != "event" or not ev.get("id"):
            continue
        eid = str(ev["id"])
        start_at = ev.get("start_at")
        cname = ev.get("context_name") or _resolve_context_name(str(ev.get("context_code") or ""), course_names)
        title = ev.get("title") or f"Event {eid}"
        url = ev.get("html_url") or f"{client.base_url}/calendar"
        summary = html_to_text(ev.get("description"))

        status = tracker.track(f"event:{eid}", digest(title, start_at, _normalize_html_for_hash(ev.get("description"))))
        urgent_emitted = tracker.check_urgent_2h(
            f"event:{eid}", start_at, cname, title, url, is_new_item=(status == "new"), summary=summary
        )
        if status == "new" and not urgent_emitted:
            tracker.notifications.append(NotificationItem("event_new", cname, title, url, due_at=start_at, summary=summary))
        elif status == "changed" and not urgent_emitted:
            tracker.notifications.append(NotificationItem("event_changed", cname, title, url, due_at=start_at, summary=summary))


def collect_updates(
    client: CanvasClient,
    state: dict[str, Any],
    now: datetime | None = None,
) -> tuple[list[NotificationItem], dict[str, Any], list[dict[str, Any]]]:
    """Fetch all LMS sections and compute updates + urgent <2h reminders."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    courses = client.get_active_courses()
    course_names = {c["id"]: c["name"] for c in courses}
    tracker = _StateTracker(state, current_time)

    for course in courses:
        cid = course["id"]
        cname = course["name"]
        _collect_course_assignments(client, tracker, cid, cname)
        _collect_course_topics(client, tracker, cid, cname)
        _collect_course_quizzes(client, tracker, cid, cname)
        _collect_course_content(client, tracker, course)

    _collect_global_and_dashboard(client, tracker, course_names)

    # Put urgent <2h items first so they are never buried at the bottom.
    ordered = sorted(tracker.notifications, key=lambda n: 0 if n.is_urgent else 1)
    return ordered, tracker.to_state_dict(), courses


def build_email_subject(items: list[NotificationItem]) -> str:
    has_urgent = any(i.is_urgent for i in items)
    prefix = "[KHẨN - UET-LMS]" if has_urgent else "[UET-LMS]"
    if len(items) == 1:
        item = items[0]
        return f"{prefix} {item.label}: {item.title} ({item.course_name})"
    return f"{prefix} Có {len(items)} cập nhật mới trên LMS"


def build_email_bodies(items: list[NotificationItem]) -> tuple[str, str]:
    """Build plain-text and HTML email bodies for detected updates."""
    text_lines = [f"Phát hiện {len(items)} cập nhật từ UET-LMS:", ""]
    html_cards: list[str] = []

    for item in items:
        time_line = item.extra_line or f"Thời gian / Deadline: {format_vn_time(item.due_at)}"
        if item.extra_line and item.due_at and item.is_urgent:
            time_line = f"{item.extra_line} (Hạn: {format_vn_time(item.due_at)})"

        text_lines.extend(
            [
                f"[{item.label}] {item.title}",
                f"Môn học / Mục: {item.course_name}",
                time_line,
                f"Link: {item.url}",
            ]
        )
        if item.summary:
            text_lines.append(f"Nội dung: {item.summary}")
        text_lines.append("")

        accent = "#d93025" if item.is_urgent else "#006bb3"
        bg = "#fff8f7" if item.is_urgent else "#ffffff"
        summary_html = (
            f"<p style='margin:8px 0 0;color:#444;font-size:14px;'>{escape(item.summary)}</p>"
            if item.summary
            else ""
        )
        html_cards.append(
            f"<div style='border:1px solid #e0e0e0;border-left:4px solid {accent};"
            f"border-radius:6px;padding:12px 16px;margin-bottom:12px;background:{bg};'>"
            f"<div style='font-size:12px;color:{accent};font-weight:bold;'>"
            f"{escape(item.label)} &bull; {escape(item.course_name)}</div>"
            f"<h3 style='margin:4px 0 6px;font-size:16px;'>"
            f"<a href='{escape(item.url)}' style='color:#111;text-decoration:none;'>"
            f"{escape(item.title)}</a></h3>"
            f"<div style='font-size:13px;color:#555;'>{escape(time_line)}</div>"
            f"{summary_html}"
            f"<div style='margin-top:8px;font-size:13px;'>"
            f"<a href='{escape(item.url)}'>Mở trên UET-LMS &rarr;</a></div>"
            "</div>"
        )

    html_body = (
        "<html><body style='font-family:Arial,sans-serif;background:#f5f7fa;padding:16px;'>"
        "<div style='max-width:640px;margin:0 auto;'>"
        f"<h2 style='color:#004c80;margin-top:0;'>Cập nhật từ UET-LMS ({len(items)})</h2>"
        + "".join(html_cards)
        + "</div></body></html>"
    )
    return "\n".join(text_lines).strip(), html_body


def send_email(
    subject: str,
    text_body: str,
    html_body: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    to_email: str,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"UET-LMS Notifier <{smtp_user}>"
    msg["To"] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=REQUEST_TIMEOUT_SEC) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=REQUEST_TIMEOUT_SEC) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)


def _require_env(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def main() -> int:
    try:
        base_url = os.getenv("CANVAS_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        token = _require_env("CANVAS_TOKEN")
        smtp_host = os.getenv("SMTP_HOST", DEFAULT_SMTP_HOST).strip() or DEFAULT_SMTP_HOST
        smtp_port = int(os.getenv("SMTP_PORT", str(DEFAULT_SMTP_PORT)).strip() or DEFAULT_SMTP_PORT)
        smtp_user = _require_env("SMTP_USER")
        smtp_pass = _require_env("SMTP_PASS")
        to_email = os.getenv("TO_EMAIL", "").strip() or smtp_user
        state_path = Path(os.getenv("STATE_FILE", "state.json"))
    except (RuntimeError, ValueError) as err:
        print(f"[ERROR] Configuration error: {err}", file=sys.stderr)
        return 1

    client = CanvasClient(base_url, token)
    state = load_state(state_path)

    try:
        notifications, next_state, courses = collect_updates(client, state)
    except HTTPError as err:
        print(f"[ERROR] Canvas API HTTP {err.code}: {err.reason}", file=sys.stderr)
        return 1
    except URLError as err:
        print(f"[ERROR] Network error reaching Canvas: {err.reason}", file=sys.stderr)
        return 1

    if not state["initialized"]:
        course_list = "\n".join(f"- {c['name']}" for c in courses) or "- (Không có môn học active)"
        init_text = (
            "UET-LMS Notifier v2 đã kết nối thành công và lưu mốc trạng thái ban đầu.\n\n"
            f"Số môn đang theo dõi ({len(courses)}):\n{course_list}\n\n"
            f"Đã ghi nhận {len(next_state['items'])} mục hiện có (bài tập, điểm, tập tin, "
            "học phần, trang, thông báo, thảo luận, hộp thư, bảng điều khiển...).\n"
            "Từ lần chạy sau (mỗi 10 phút), mọi thay đổi hoặc deadline < 2 tiếng chưa nộp sẽ được gửi ngay về email này."
        )
        init_html = (
            "<html><body style='font-family:Arial,sans-serif;'>"
            "<h2 style='color:#004c80;'>UET-LMS Notifier đã kích hoạt thành công</h2>"
            f"<p>Đang theo dõi <b>{len(courses)}</b> môn học active:</p>"
            "<ul>" + "".join(f"<li>{escape(c['name'])}</li>" for c in courses) + "</ul>"
            f"<p>Đã lưu mốc ban đầu (<b>{len(next_state['items'])}</b> mục hiện có dưới dạng mã băm bảo mật). "
            "Từ lần kiểm tra tiếp theo (mỗi 10 phút), mọi thay đổi mới và nhắc hạn &lt; 2 tiếng chưa nộp sẽ được gửi về email của bạn.</p>"
            "</body></html>"
        )
        try:
            send_email(
                "[UET-LMS] Kích hoạt theo dõi toàn diện LMS thành công",
                init_text,
                init_html,
                smtp_host,
                smtp_port,
                smtp_user,
                smtp_pass,
                to_email,
            )
            if notifications:
                send_email(
                    build_email_subject(notifications),
                    *build_email_bodies(notifications),
                    smtp_host,
                    smtp_port,
                    smtp_user,
                    smtp_pass,
                    to_email,
                )
        except (smtplib.SMTPException, OSError) as err:
            print(f"[ERROR] Failed to send confirmation email: {err}", file=sys.stderr)
            return 1
        save_state(state_path, next_state)
        print(f"[INIT] Seeded baseline state ({len(courses)} courses, {len(next_state['items'])} tracked items).")
        return 0

    if notifications:
        subject = build_email_subject(notifications)
        text_body, html_body = build_email_bodies(notifications)
        try:
            send_email(
                subject,
                text_body,
                html_body,
                smtp_host,
                smtp_port,
                smtp_user,
                smtp_pass,
                to_email,
            )
        except (smtplib.SMTPException, OSError) as err:
            print(f"[ERROR] Failed to send notification email: {err}", file=sys.stderr)
            return 1
        print(f"[OK] Sent email with {len(notifications)} update(s) to {to_email}.")
    else:
        print("[OK] No new updates or <2h deadlines.")

    save_state(state_path, next_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
