"""
YouTube Data API v3 - 댓글 수집 스크립트
========================================
검색 키워드로 영상을 자동 탐색하고 댓글을 수집합니다.

사용법:
  1. Google Cloud Console에서 YouTube Data API v3 키 발급
  2. API 키 설정: export YOUTUBE_API_KEY="YOUR_API_KEY"
  3. 실행: python youtube_comments.py
     또는: python youtube_comments.py "검색어1" "검색어2" ...

참고:
  - YouTube Data API v3 무료 할당량: 일 10,000 units
  - search.list = 100 units/call, commentThreads.list = 1 unit/call
"""

import csv
import json
import os
import sys
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

# ============================================================
# 설정
# ============================================================

# API 키: 환경변수 또는 직접 입력
API_KEY = os.environ.get("YOUTUBE_API_KEY", "YOUR_API_KEY_HERE")

# 검색 키워드 (커맨드라인 인자가 없을 때 기본값)
DEFAULT_SEARCH_QUERIES = [
    "붉은사막 리뷰",
    "붉은사막 후기",
    "Crimson Desert review",
]

# 검색당 가져올 영상 수
SEARCH_MAX_RESULTS = 10

# 영상당 가져올 최대 댓글 수 (최대 100/페이지, 페이지네이션으로 더 가능)
MAX_COMMENTS_PER_VIDEO = 100

# 댓글 정렬: "relevance" (인기순) 또는 "time" (최신순)
COMMENT_ORDER = "relevance"

# ============================================================
# API 호출 함수들
# ============================================================

BASE_URL = "https://www.googleapis.com/youtube/v3"


def api_request(endpoint: str, params: dict) -> dict:
    """YouTube Data API v3 요청"""
    params["key"] = API_KEY
    url = f"{BASE_URL}/{endpoint}?{urlencode(params)}"

    req = Request(url)
    req.add_header("Accept", "application/json")

    try:
        with urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"\n❌ API 오류 (HTTP {e.code}):")
        try:
            error_json = json.loads(error_body)
            error_msg = error_json.get("error", {}).get("message", error_body)
            error_reason = (
                error_json.get("error", {})
                .get("errors", [{}])[0]
                .get("reason", "unknown")
            )
            print(f"   이유: {error_reason}")
            print(f"   메시지: {error_msg}")

            if error_reason == "forbidden" or "API key" in error_msg:
                print("\n💡 해결 방법:")
                print("   1. Google Cloud Console 접속: https://console.cloud.google.com")
                print("   2. YouTube Data API v3 활성화 확인")
                print("   3. API 키가 올바른지 확인")
                print("   4. API 키 제한사항 확인 (IP/리퍼러 제한 등)")
            elif error_reason == "commentsDisabled":
                print("   → 이 영상은 댓글이 비활성화되어 있습니다.")
        except json.JSONDecodeError:
            print(f"   {error_body}")
        return None
    except URLError as e:
        print(f"\n❌ 네트워크 오류: {e.reason}")
        return None


def search_videos(query: str, max_results: int = 10) -> list:
    """
    YouTube 검색으로 영상을 찾는다.

    Parameters:
        query: 검색 키워드
        max_results: 가져올 영상 수 (최대 50)

    Returns:
        list of dict: [{"id": "...", "title": "...", "channel": "..."}, ...]

    API 비용: 100 units/call
    """
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": min(max_results, 50),
        "order": "relevance",
    }
    data = api_request("search", params)
    if data is None:
        return []

    videos = []
    for item in data.get("items", []):
        videos.append({
            "id": item["id"]["videoId"],
            "title": item["snippet"]["title"],
            "channel": item["snippet"]["channelTitle"],
        })
    return videos


def get_comment_threads(
    video_id: str,
    max_results: int = 100,
    order: str = "relevance",
    page_token: str = None,
) -> dict:
    """
    영상의 댓글 스레드(최상위 댓글 + 답글) 가져오기

    Parameters:
        video_id: YouTube 영상 ID
        max_results: 가져올 댓글 수 (최대 100)
        order: 정렬 - "relevance"(인기순) / "time"(최신순)
        page_token: 페이지네이션 토큰

    API 비용: 1 unit/call
    """
    params = {
        "part": "snippet,replies",
        "videoId": video_id,
        "maxResults": min(max_results, 100),
        "order": order,
        "textFormat": "plainText",
    }
    if page_token:
        params["pageToken"] = page_token

    return api_request("commentThreads", params)


def collect_comments_for_video(
    video_id: str, title: str, max_comments: int = 100, order: str = "relevance"
) -> list:
    """
    한 영상의 댓글을 수집하여 정리된 리스트로 반환

    Returns:
        list of dict: 각 댓글 정보
        [
            {
                "author": "닉네임",
                "text": "댓글 내용",
                "likes": 123,
                "published": "2026-03-20T...",
                "is_reply": False,
                "reply_to": None,
                "replies": [
                    {"author": "...", "text": "...", ...}
                ]
            },
            ...
        ]
    """
    all_comments = []
    page_token = None
    collected = 0

    while collected < max_comments:
        fetch_count = min(100, max_comments - collected)
        data = get_comment_threads(video_id, fetch_count, order, page_token)

        if data is None:
            break

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            # 최상위 댓글
            top = item["snippet"]["topLevelComment"]["snippet"]
            comment = {
                "author": top["authorDisplayName"],
                "text": top["textDisplay"],
                "likes": top.get("likeCount", 0),
                "published": top["publishedAt"],
                "updated": top.get("updatedAt", top["publishedAt"]),
                "is_reply": False,
                "reply_to": None,
                "replies": [],
            }

            # 답글이 있는 경우
            if "replies" in item:
                for reply_item in item["replies"]["comments"]:
                    r = reply_item["snippet"]
                    comment["replies"].append(
                        {
                            "author": r["authorDisplayName"],
                            "text": r["textDisplay"],
                            "likes": r.get("likeCount", 0),
                            "published": r["publishedAt"],
                            "is_reply": True,
                            "reply_to": top["authorDisplayName"],
                        }
                    )

            all_comments.append(comment)
            collected += 1

        # 다음 페이지
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return all_comments


# ============================================================
# 핵심 로직 — 외부에서 호출 가능
# ============================================================


def collect(queries: list, output_dir: str = ".", max_results: int = 10,
            max_comments: int = 100, comment_order: str = "relevance",
            progress=None) -> dict:
    """
    댓글 수집 핵심 로직.

    Parameters:
        queries: 검색 키워드 리스트
        output_dir: 출력 파일 저장 디렉토리
        max_results: 검색당 가져올 영상 수
        max_comments: 영상당 최대 댓글 수
        comment_order: 댓글 정렬 ("relevance" 또는 "time")
        progress: (message: str) → None 콜백 (없으면 print)

    Returns:
        dict: 수집 결과 (video_id → video data)
    """
    log = progress if progress else print

    os.makedirs(output_dir, exist_ok=True)

    log(f"검색 키워드: {queries}")
    log(f"검색당 최대 영상: {max_results}개 / 영상당 최대 댓글: {max_comments}개")

    # 1단계: 영상 검색
    log("영상 검색 중...")
    seen_ids = set()
    target_videos = []
    for query in queries:
        log(f'  "{query}" 검색 중...')
        found = search_videos(query, max_results)
        new_count = 0
        for v in found:
            if v["id"] not in seen_ids:
                seen_ids.add(v["id"])
                target_videos.append(v)
                new_count += 1
        log(f'  "{query}" → {len(found)}개 발견 (신규 {new_count}개)')

    if not target_videos:
        log("검색 결과가 없습니다.")
        return {}

    log(f"총 {len(target_videos)}개 영상 대상")

    # 2단계: 댓글 수집
    log("댓글 수집 시작...")
    results = {}
    total_comments = 0

    for i, video in enumerate(target_videos, 1):
        vid = video["id"]
        title = f"{video['channel']} - {video['title']}"
        url = f"https://www.youtube.com/watch?v={vid}"

        log(f"[{i}/{len(target_videos)}] {title}")

        comments = collect_comments_for_video(
            vid, title, max_comments, comment_order
        )

        reply_count = sum(len(c["replies"]) for c in comments)
        log(f"  → 댓글 {len(comments)}개 (답글 {reply_count}개 포함)")

        results[vid] = {
            "video_id": vid,
            "title": title,
            "channel": video.get("channel", ""),
            "url": url,
            "collected_at": datetime.now().isoformat(),
            "comment_count": len(comments),
            "comments": comments,
        }
        total_comments += len(comments)

    # JSON 저장
    output_json = os.path.join(output_dir, "comments_output.json")
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log(f"JSON 저장 완료: {output_json}")

    # 읽기 좋은 텍스트 저장
    output_txt = os.path.join(output_dir, "comments_output_readable.txt")
    with open(output_txt, "w", encoding="utf-8") as f:
        f.write("=" * 70 + "\n")
        f.write("유튜브 댓글 수집 결과\n")
        f.write(f"검색 키워드: {queries}\n")
        f.write(f"수집 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"총 영상 수: {len(target_videos)}개\n")
        f.write(f"총 댓글 수: {total_comments}개\n")
        f.write("=" * 70 + "\n\n")

        for vid, data in results.items():
            f.write("─" * 70 + "\n")
            f.write(f"📺 {data['title']}\n")
            f.write(f"🔗 {data['url']}\n")
            f.write(f"💬 댓글 {data['comment_count']}개\n")
            f.write("─" * 70 + "\n\n")

            for j, comment in enumerate(data["comments"], 1):
                likes_str = f" [👍 {comment['likes']}]" if comment["likes"] > 0 else ""
                f.write(f"  [{j}] {comment['author']}{likes_str}\n")
                for line in comment["text"].split("\n"):
                    f.write(f"      {line}\n")

                for reply in comment["replies"]:
                    r_likes = (
                        f" [👍 {reply['likes']}]" if reply["likes"] > 0 else ""
                    )
                    f.write(f"\n    ↳ {reply['author']}{r_likes}\n")
                    for line in reply["text"].split("\n"):
                        f.write(f"        {line}\n")

                f.write("\n")

            f.write("\n")

    log(f"텍스트 저장 완료: {output_txt}")

    # CSV 저장
    output_csv = os.path.join(output_dir, "comments_output.csv")
    with open(output_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "video_id", "video_title", "video_url",
            "author", "text", "likes", "published", "updated",
            "is_reply", "reply_to",
        ])
        for vid, data in results.items():
            for comment in data["comments"]:
                writer.writerow([
                    data["video_id"], data["title"], data["url"],
                    comment["author"], comment["text"], comment["likes"],
                    comment["published"], comment["updated"],
                    False, "",
                ])
                for reply in comment["replies"]:
                    writer.writerow([
                        data["video_id"], data["title"], data["url"],
                        reply["author"], reply["text"], reply["likes"],
                        reply["published"], "",
                        True, reply["reply_to"],
                    ])
    log(f"CSV 저장 완료: {output_csv}")

    log(f"수집 완료! 총 영상: {len(results)}개, 총 댓글: {total_comments}개")

    return results


# ============================================================
# CLI 메인 실행
# ============================================================


def main():
    # API 키 확인
    if API_KEY == "YOUR_API_KEY_HERE":
        print("⚠️  YouTube API 키가 설정되지 않았습니다!")
        print("   export YOUTUBE_API_KEY=\"AIza...\" 설정 후 재실행하세요.")
        sys.exit(1)

    # 검색 키워드: 커맨드라인 인자 또는 기본값
    queries = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_SEARCH_QUERIES

    print("=" * 60)
    print("🎮 유튜브 댓글 수집기")
    print(f"   실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    results = collect(
        queries=queries,
        output_dir=".",
        max_results=SEARCH_MAX_RESULTS,
        max_comments=MAX_COMMENTS_PER_VIDEO,
        comment_order=COMMENT_ORDER,
    )

    if not results:
        print("\n❌ 검색 결과가 없습니다.")
        sys.exit(1)

    # 미리보기: 각 영상별 인기 댓글 Top 3
    print("\n📌 영상별 인기 댓글 Top 3 미리보기:")
    for vid, data in results.items():
        if not data["comments"]:
            continue
        print(f"\n  📺 {data['title']}")
        sorted_comments = sorted(
            data["comments"], key=lambda x: x["likes"], reverse=True
        )
        for k, c in enumerate(sorted_comments[:3], 1):
            text_preview = c["text"][:80].replace("\n", " ")
            if len(c["text"]) > 80:
                text_preview += "..."
            print(f"    {k}. [👍{c['likes']}] {c['author']}: {text_preview}")


if __name__ == "__main__":
    main()
