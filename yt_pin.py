import os
import re
import html
import base64
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

GMAIL_SCOPES   = ["https://www.googleapis.com/auth/gmail.modify"]
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

TARGET_COMMENT = """▼지금 바로 수행기사로 취업해서 월 400 이상 벌고 싶다면 ▼
https://youtu.be/ZjgoXC8p_Ps?si=ZSkeZhMJ4kMnHjty""".strip()

SENDER_FILTER = "bearchauffeur@vbconsulting.biz"
VIDEO_ID_RE = re.compile(r"https://www\.youtube\.com/watch\?v=([a-zA-Z0-9_-]+)")


# ── 인증 ─────────────────────────────────────────────────────────────────────

def get_credentials(token_file, scopes):
    """token_file과 scopes에 맞는 인증 객체 반환. 갱신 시 token_file 덮어씀."""
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", scopes)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return creds


# ── 텍스트 비교 ───────────────────────────────────────────────────────────────

def normalize(text):
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(text.split()).strip()

def text_matches(text, target):
    # == 완전 일치 사용 금지 — HTML 엔티티 차이로 오탐 발생
    return normalize(target) in normalize(text)


# ── YouTube 댓글 ──────────────────────────────────────────────────────────────

def get_my_channel_id(youtube):
    resp = youtube.channels().list(part="id", mine=True).execute()
    return resp["items"][0]["id"]


def find_my_comment(youtube, video_id, target_text, my_channel_id):
    """내 채널의 댓글 중 target_text가 포함된 댓글이 있으면 True 반환."""
    try:
        page_token = None
        while True:
            resp = youtube.commentThreads().list(
                part="snippet",
                videoId=video_id,
                maxResults=100,
                pageToken=page_token,
            ).execute()
            for item in resp.get("items", []):
                top = item["snippet"]["topLevelComment"]["snippet"]
                author = top.get("authorChannelId", {}).get("value", "")
                if author != my_channel_id:
                    continue
                if text_matches(top.get("textOriginal", ""), target_text) or \
                   text_matches(top.get("textDisplay", ""), target_text):
                    return True
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        err = str(e)
        if "commentsDisabled" in err or "403" in err or "disabled comments" in err.lower():
            print(f"  ⚠ 댓글 비활성화 또는 접근 불가 ({video_id}) — 건너뜀")
            return True
        raise
    return False


def get_video_info(youtube, video_id):
    """영상 제목, 게시일, Shorts 여부 반환."""
    resp = youtube.videos().list(
        part="contentDetails,snippet",
        id=video_id,
    ).execute()
    items = resp.get("items", [])
    if not items:
        return {"is_shorts": False, "title": "", "published_at": ""}
    item = items[0]
    duration = item["contentDetails"]["duration"]
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    seconds = 0
    if match:
        h, m, s = (int(x or 0) for x in match.groups())
        seconds = h * 3600 + m * 60 + s
    return {
        "is_shorts": seconds <= 60,
        "title": item["snippet"].get("title", ""),
        "published_at": item["snippet"].get("publishedAt", "")[:10],
    }


def add_comment(youtube, video_id, text):
    resp = youtube.commentThreads().insert(
        part="snippet",
        body={
            "snippet": {
                "videoId": video_id,
                "topLevelComment": {
                    "snippet": {"textOriginal": text}
                },
            }
        },
    ).execute()
    return resp["snippet"]["topLevelComment"]["id"]


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_email_body(gmail, msg_id):
    msg = gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()

    def extract(payload):
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        for part in payload.get("parts", []):
            text = extract(part)
            if text:
                return text
        return ""

    return extract(msg["payload"])


def send_comment_notification(video_id, title, published_at, config):
    email_cfg = config.get("email", {})
    sender = email_cfg.get("sender", "")
    password = email_cfg.get("password", "")
    recipients = email_cfg.get("recipients", [])
    channel_name = config.get("channel_name", "")
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"[{channel_name}] 고정 댓글 추가 완료"
    body = (
        f"다음 영상에 고정 댓글이 추가되었습니다.\n\n"
        f"제목: {title}\n"
        f"업로드 일자: {published_at}\n"
        f"영상 링크: {video_url}"
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        print(f"    → 댓글 알림 이메일 발송 완료")
    except Exception as e:
        print(f"    → 댓글 알림 이메일 발송 실패: {e}")


def update_notion_comment_flag(video_id, config):
    token = config.get("notion_token", "")
    database_id = config.get("notion_database_id", "")
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    resp = requests.post(
        f"https://api.notion.com/v1/databases/{database_id}/query",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
        json={"filter": {"property": "영상", "url": {"equals": video_url}}},
        timeout=30,
    )
    results = resp.json().get("results", [])
    if not results:
        print(f"    → Notion 페이지 없음 — 건너뜀")
        return

    page_id = results[0]["id"]
    resp = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
        json={"properties": {"고정댓글": {"checkbox": True}}},
        timeout=30,
    )
    if resp.status_code == 200:
        print(f"    → Notion 고정댓글 체크 완료")
    else:
        print(f"    → Notion 업데이트 실패 ({resp.status_code}): {resp.text}")


def mark_as_read(gmail, msg_id):
    gmail.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    with open("monitor_config.json") as f:
        config = json.load(f)

    gmail_creds   = get_credentials("gmail_token.json",   GMAIL_SCOPES)
    youtube_creds = get_credentials("youtube_token.json", YOUTUBE_SCOPES)

    gmail   = build("gmail",   "v1", credentials=gmail_creds)
    youtube = build("youtube", "v3", credentials=youtube_creds)

    my_channel_id = get_my_channel_id(youtube)
    print(f"YouTube 채널 ID: {my_channel_id}")

    # 읽지 않은 알림 이메일 조회
    query = f"from:{SENDER_FILTER} is:unread"
    result = gmail.users().messages().list(userId="me", q=query).execute()
    messages = result.get("messages", [])

    if not messages:
        print("처리할 새 알림 이메일이 없습니다.")
        return

    print(f"미읽음 이메일 {len(messages)}개 발견")

    for msg in messages:
        msg_id = msg["id"]
        body = get_email_body(gmail, msg_id)

        video_ids = list(dict.fromkeys(VIDEO_ID_RE.findall(body)))  # 중복 제거, 순서 유지
        if not video_ids:
            print(f"  이메일 {msg_id}: 영상 ID 없음 — 읽음 처리")
            mark_as_read(gmail, msg_id)
            continue

        print(f"  이메일 {msg_id}: 영상 {len(video_ids)}개 추출 → {video_ids}")

        for vid in video_ids:
            info = get_video_info(youtube, vid)
            if info["is_shorts"]:
                print(f"    [{vid}] Shorts — 건너뜀")
                continue
            print(f"    [{vid}] 댓글 확인 중...")
            if find_my_comment(youtube, vid, TARGET_COMMENT, my_channel_id):
                print(f"    [{vid}] 이미 존재 — 건너뜀")
            else:
                add_comment(youtube, vid, TARGET_COMMENT)
                print(f"    [{vid}] 댓글 추가 완료 ✓")
                update_notion_comment_flag(vid, config)
                send_comment_notification(vid, info["title"], info["published_at"], config)

        mark_as_read(gmail, msg_id)
        print(f"  이메일 {msg_id} 읽음 처리 완료")

    print("완료")


if __name__ == "__main__":
    main()
