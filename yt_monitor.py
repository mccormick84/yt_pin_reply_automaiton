import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

CONFIG_FILE = "monitor_config.json"
STATE_FILE = "last_video_id.txt"


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def parse_duration_seconds(iso_duration):
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
    if not match:
        return 0
    h, m, s = (int(x or 0) for x in match.groups())
    return h * 3600 + m * 60 + s


def get_channel_id(handle, api_key):
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "id", "forHandle": handle, "key": api_key},
        timeout=10,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    if not items:
        raise RuntimeError(f"채널을 찾을 수 없습니다: @{handle}")
    return items[0]["id"]


def get_latest_videos(channel_id, api_key, max_results=10):
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "channelId": channel_id,
            "order": "date",
            "type": "video",
            "maxResults": max_results,
            "key": api_key,
        },
        timeout=10,
    )
    resp.raise_for_status()
    videos = []
    for item in resp.json().get("items", []):
        vid = item["id"]["videoId"]
        published_at = item["snippet"].get("publishedAt", "")[:10]
        videos.append({
            "id": vid,
            "title": item["snippet"]["title"],
            "url": f"https://www.youtube.com/watch?v={vid}",
            "comments_url": f"https://www.youtube.com/watch?v={vid}#comments",
            "published_at": published_at,
            "type": "일반",
        })
    return videos


def enrich_with_duration(videos, api_key):
    if not videos:
        return videos
    ids = [v["id"] for v in videos]
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "contentDetails", "id": ",".join(ids), "key": api_key},
        timeout=10,
    )
    resp.raise_for_status()
    duration_map = {
        item["id"]: parse_duration_seconds(item["contentDetails"]["duration"])
        for item in resp.json().get("items", [])
    }
    for video in videos:
        seconds = duration_map.get(video["id"], 999)
        video["type"] = "쇼츠" if seconds <= 60 else "일반"
    return videos


def add_to_notion(video, token, database_id):
    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
        json={
            "parent": {"database_id": database_id},
            "properties": {
                "제목": {"title": [{"text": {"content": video["title"]}}]},
                "영상": {"url": video["url"]},
                "댓글": {"url": video["comments_url"]},
                "게시일": {"date": {"start": video["published_at"]}},
                "구분": {"select": {"name": video["type"]}},
            },
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  Notion 추가 실패 ({resp.status_code}): {resp.text}")
        return False
    return True


def send_email(new_videos, config):
    email_cfg = config.get("email", {})
    sender = email_cfg.get("sender", "")
    password = email_cfg.get("password", "")
    recipients = email_cfg.get("recipients", [])
    channel_name = config.get("channel_name", config.get("channel_handle"))
    fixed_comment = email_cfg.get("fixed_comment", "")

    video_links = "\n".join(f"{v['url']} ({v['title']})" for v in new_videos)
    body = (
        f"{channel_name} 채널에 새로운 영상이 게시되었습니다.\n"
        f"고정 댓글을 추가해주세요.\n\n"
        f"영상 링크\n"
        f"{video_links}\n\n"
        f"<고정 댓글>\n"
        f"{fixed_comment}"
    )

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"[{channel_name}] 새 영상 알림"
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        print(f"  → 이메일 발송 완료 ({', '.join(recipients)})")
    except Exception as e:
        print(f"  이메일 발송 실패: {e}")


def main():
    config = load_config()
    api_key = config["youtube_api_key"]
    notion_token = config["notion_token"]
    notion_db_id = config["notion_database_id"]
    handle = config["channel_handle"]

    last_video_id = os.environ.get("LAST_VIDEO_ID", "").strip() or None

    channel_id = get_channel_id(handle, api_key)
    videos = get_latest_videos(channel_id, api_key)
    if not videos:
        print("영상 목록을 가져올 수 없습니다.")
        return

    latest_id = videos[0]["id"]

    if last_video_id is None:
        with open(STATE_FILE, "w") as f:
            f.write(latest_id)
        print(f"초기 상태 저장 완료. 최신 영상: {videos[0]['title']}")
        return

    new_videos = []
    for video in videos:
        if video["id"] == last_video_id:
            break
        new_videos.append(video)

    if not new_videos:
        print("새로운 영상이 없습니다.")
        with open(STATE_FILE, "w") as f:
            f.write(latest_id)
        return

    enrich_with_duration(new_videos, api_key)

    for video in reversed(new_videos):
        print(f"새 영상 발견 [{video['type']}]: {video['title']}")
        if add_to_notion(video, notion_token, notion_db_id):
            print(f"  → Notion 추가 완료")

    send_email(list(reversed(new_videos)), config)

    with open(STATE_FILE, "w") as f:
        f.write(latest_id)
    print(f"총 {len(new_videos)}개의 새 영상을 처리했습니다.")


if __name__ == "__main__":
    main()
