"""UET-LMS (Canvas) Notifier: polls Canvas REST API and sends email alerts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
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


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML-to-text converter for Canvas descriptions."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li"}:
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
    if len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text


def format_vn_time(iso_str: str | None) -> str:
    """Convert Canvas ISO-8601 UTC timestamp to readable UTC+7 string."""
    if not iso_str:
        return "Không có"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(VN_TZ).strftime("%H:%M %d/%m/%Y (UTC+7)")
    except ValueError:
        return iso_str


@dataclass(frozen=True)
class NotificationItem:
    kind: str  # "assignment_new" | "assignment_due_changed" | "announcement" | "event_new" | "event_changed"
    course_name: str
    title: str
    url: str
    due_at: str | None = None
    old_due_at: str | None = None
    summary: str = ""

    @property
    def label(self) -> str:
        labels = {
            "assignment_new": "Bài tập mới",
            "assignment_due_changed": "Cập nhật Deadline",
            "announcement": "Thông báo mới",
            "event_new": "Lịch / Deadline mới",
            "event_changed": "Cập nhật Lịch",
        }
        return labels.get(self.kind, "Cập nhật LMS")


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
                "User-Agent": "uet-lms-notifier/1.0",
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

    def get_active_courses(self) -> list[dict[str, Any]]:
        courses = self.get_paginated(
            "/api/v1/courses",
            {"enrollment_state": "active", "per_page": 100},
        )
        return [c for c in courses if c.get("id") and c.get("name")]

    def get_course_assignments(self, course_id: int) -> list[dict[str, Any]]:
        return self._safe_course_list(
            f"/api/v1/courses/{course_id}/assignments",
            {"per_page": 100},
        )

    def get_course_announcements(self, course_id: int) -> list[dict[str, Any]]:
        return self._safe_course_list(
            f"/api/v1/courses/{course_id}/discussion_topics",
            {"only_announcements": "true", "per_page": 50},
        )

    def get_upcoming_events(self) -> list[dict[str, Any]]:
        return self.get_paginated("/api/v1/users/self/upcoming_events")

    def _safe_course_list(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return self.get_paginated(path, params)
        except HTTPError as err:
            # Some courses disable specific tabs (403/404); skip without failing the run.
            if err.code in {403, 404}:
                print(f"[WARN] Skipping {path} (HTTP {err.code})", file=sys.stderr)
                return []
            raise


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"initialized": False, "assignments": {}, "announcements": {}, "events": {}}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        "initialized": bool(data.get("initialized", False)),
        "assignments": dict(data.get("assignments", {})),
        "announcements": dict(data.get("announcements", {})),
        "events": dict(data.get("events", {})),
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def collect_updates(
    client: CanvasClient,
    state: dict[str, Any],
) -> tuple[list[NotificationItem], dict[str, Any], list[dict[str, Any]]]:
    """Fetch current LMS data and compute diff against stored state."""
    courses = client.get_active_courses()
    course_names = {c["id"]: c["name"] for c in courses}

    new_assignments: dict[str, str | None] = dict(state["assignments"])
    new_announcements: dict[str, str | None] = dict(state["announcements"])
    new_events: dict[str, str | None] = dict(state["events"])
    notifications: list[NotificationItem] = []

    for course in courses:
        cid = course["id"]
        cname = course["name"]

        for item in client.get_course_assignments(cid):
            if item.get("published") is False:
                continue
            aid = str(item["id"])
            due_at = item.get("due_at")
            url = item.get("html_url") or f"{client.base_url}/courses/{cid}/assignments/{aid}"
            summary = html_to_text(item.get("description"))

            if aid not in new_assignments:
                notifications.append(
                    NotificationItem(
                        kind="assignment_new",
                        course_name=cname,
                        title=item.get("name") or f"Assignment {aid}",
                        url=url,
                        due_at=due_at,
                        summary=summary,
                    )
                )
            elif new_assignments[aid] != due_at:
                notifications.append(
                    NotificationItem(
                        kind="assignment_due_changed",
                        course_name=cname,
                        title=item.get("name") or f"Assignment {aid}",
                        url=url,
                        due_at=due_at,
                        old_due_at=new_assignments[aid],
                        summary=summary,
                    )
                )
            new_assignments[aid] = due_at

        for topic in client.get_course_announcements(cid):
            if topic.get("published") is False:
                continue
            tid = str(topic["id"])
            if tid not in new_announcements:
                url = topic.get("html_url") or f"{client.base_url}/courses/{cid}/announcements/{tid}"
                notifications.append(
                    NotificationItem(
                        kind="announcement",
                        course_name=cname,
                        title=topic.get("title") or f"Announcement {tid}",
                        url=url,
                        due_at=topic.get("posted_at"),
                        summary=html_to_text(topic.get("message")),
                    )
                )
            new_announcements[tid] = topic.get("posted_at")

    for ev in client.get_upcoming_events():
        if ev.get("type") != "event" or not ev.get("id"):
            continue
        eid = str(ev["id"])
        start_at = ev.get("start_at")
        context_code = ev.get("context_code", "")
        cname = ev.get("context_name") or _resolve_context_name(context_code, course_names)
        url = ev.get("html_url") or f"{client.base_url}/calendar"
        summary = html_to_text(ev.get("description"))

        if eid not in new_events:
            notifications.append(
                NotificationItem(
                    kind="event_new",
                    course_name=cname,
                    title=ev.get("title") or f"Event {eid}",
                    url=url,
                    due_at=start_at,
                    summary=summary,
                )
            )
        elif new_events[eid] != start_at:
            notifications.append(
                NotificationItem(
                    kind="event_changed",
                    course_name=cname,
                    title=ev.get("title") or f"Event {eid}",
                    url=url,
                    due_at=start_at,
                    old_due_at=new_events[eid],
                    summary=summary,
                )
            )
        new_events[eid] = start_at

    next_state = {
        "initialized": True,
        "assignments": new_assignments,
        "announcements": new_announcements,
        "events": new_events,
    }
    return notifications, next_state, courses


def _resolve_context_name(context_code: str, course_names: dict[int, str]) -> str:
    if context_code.startswith("course_"):
        try:
            cid = int(context_code.split("_", 1)[1])
            return course_names.get(cid, context_code)
        except ValueError:
            pass
    return context_code or "UET-LMS"


def build_email_subject(items: list[NotificationItem]) -> str:
    if len(items) == 1:
        item = items[0]
        return f"[UET-LMS] {item.label}: {item.title} ({item.course_name})"
    return f"[UET-LMS] Có {len(items)} cập nhật mới trên LMS"


def build_email_bodies(items: list[NotificationItem]) -> tuple[str, str]:
    """Build plain-text and HTML email bodies for detected updates."""
    text_lines = [f"Phát hiện {len(items)} cập nhật mới trên UET-LMS:", ""]
    html_cards: list[str] = []

    for item in items:
        time_line = f"Thời gian / Deadline: {format_vn_time(item.due_at)}"
        if item.kind in {"assignment_due_changed", "event_changed"}:
            time_line = (
                f"Deadline cũ: {format_vn_time(item.old_due_at)} -> "
                f"Mới: {format_vn_time(item.due_at)}"
            )

        text_lines.extend(
            [
                f"[{item.label}] {item.title}",
                f"Môn học: {item.course_name}",
                time_line,
                f"Link: {item.url}",
            ]
        )
        if item.summary:
            text_lines.append(f"Nội dung: {item.summary}")
        text_lines.append("")

        summary_html = (
            f"<p style='margin:8px 0 0;color:#444;font-size:14px;'>{escape(item.summary)}</p>"
            if item.summary
            else ""
        )
        html_cards.append(
            "<div style='border:1px solid #e0e0e0;border-left:4px solid #006bb3;"
            "border-radius:6px;padding:12px 16px;margin-bottom:12px;background:#fff;'>"
            f"<div style='font-size:12px;color:#006bb3;font-weight:bold;'>"
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
        f"<h2 style='color:#004c80;margin-top:0;'>Cập nhật mới từ UET-LMS ({len(items)})</h2>"
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
            "UET-LMS Notifier đã kết nối thành công và lưu trạng thái ban đầu.\n\n"
            f"Số môn đang theo dõi ({len(courses)}):\n{course_list}\n\n"
            f"Đã ghi nhận {len(next_state['assignments'])} bài tập, "
            f"{len(next_state['announcements'])} thông báo, "
            f"{len(next_state['events'])} sự kiện hiện có (không gửi mail lại các mục cũ).\n"
            "Từ lần chạy sau, mọi bài tập / deadline / thông báo mới sẽ được gửi ngay về email này."
        )
        init_html = (
            "<html><body style='font-family:Arial,sans-serif;'>"
            "<h2 style='color:#004c80;'>UET-LMS Notifier đã kích hoạt thành công</h2>"
            f"<p>Đang theo dõi <b>{len(courses)}</b> môn học active:</p>"
            "<ul>" + "".join(f"<li>{escape(c['name'])}</li>" for c in courses) + "</ul>"
            f"<p>Đã lưu mốc ban đầu ({len(next_state['assignments'])} bài tập, "
            f"{len(next_state['announcements'])} thông báo, {len(next_state['events'])} sự kiện). "
            "Từ lần kiểm tra tiếp theo, mọi thay đổi mới sẽ được gửi về email của bạn.</p>"
            "</body></html>"
        )
        try:
            send_email(
                "[UET-LMS] Kích hoạt theo dõi LMS thành công",
                init_text,
                init_html,
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
        print(f"[INIT] Seeded baseline state ({len(courses)} courses) and sent confirmation email.")
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
        print("[OK] No new assignments, deadlines, or announcements.")

    save_state(state_path, next_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
