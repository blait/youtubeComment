"""
유튜브 댓글 감성 분석 + 대시보드 생성 (Bedrock AI 기반)
================================================================
1단계: 인기 댓글 샘플을 AI에게 보내 카테고리를 발견
2단계: 전체 댓글을 배치로 AI에게 보내 분류
3단계: 결과를 dashboard.html로 생성
"""

import argparse
import json
import html as html_mod
import re
import time
import sys
import csv
import os
from collections import defaultdict

import boto3

BEDROCK_MODEL = "us.anthropic.claude-opus-4-6-v1"
BEDROCK_REGION = "us-east-1"

client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)


def call_bedrock(system: str, user: str, max_tokens: int = 4096) -> str:
    """Bedrock Claude API 호출"""
    resp = client.invoke_model(
        modelId=BEDROCK_MODEL,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }),
    )
    result = json.loads(resp["body"].read())
    return result["content"][0]["text"]


# ============================================================
# 1단계: 댓글에서 카테고리 자동 발견
# ============================================================

def discover_categories(comments: list, game_name_ko: str = "붉은사막",
                        game_name_en: str = "Crimson Desert",
                        developer: str = "Pearl Abyss",
                        progress=None) -> dict:
    """인기 댓글 샘플을 AI에게 보내서 카테고리를 발견한다."""
    log = progress if progress else print

    # 좋아요 기준 상위 댓글 + 랜덤 샘플
    popular = sorted(comments, key=lambda c: c["likes"], reverse=True)[:150]

    # 한국어/영어 분리
    ko = [c for c in popular if len(re.findall(r'[가-힣]', c["text"])) / max(len(c["text"]), 1) > 0.15]
    en = [c for c in popular if c not in ko]

    sample_text = ""
    for i, c in enumerate(ko[:80]):
        sample_text += f"[KO-{i+1}] (👍{c['likes']}) {c['text'][:300]}\n"
    for i, c in enumerate(en[:70]):
        sample_text += f"[EN-{i+1}] (👍{c['likes']}) {c['text'][:300]}\n"

    system = f"""You are a data analyst specializing in gaming community sentiment analysis.
You analyze YouTube comments about the game "{game_name_en}" ({game_name_ko}) by {developer}.
You must respond ONLY with valid JSON, no other text."""

    prompt = f"""아래는 {game_name_ko}({game_name_en}) 유튜브 리뷰 영상의 인기 댓글 샘플입니다.
이 댓글들을 읽고, 사람들이 공통적으로 이야기하는 주제를 발견해주세요.

요구사항:
1. 부정적 주제(negative_topics)와 긍정적 주제(positive_topics)를 각각 찾아주세요
2. 각 주제에는 name(한국어), description(한국어, 1문장), keywords(한국어+영어 혼합, 이 주제를 식별할 수 있는 키워드 10~15개) 를 포함
3. 댓글에서 실제로 반복 등장하는 주제만 뽑으세요 (최소 5개 이상의 댓글에서 언급)
4. 주제 수는 정하지 않습니다. 데이터에서 보이는 만큼 자유롭게 뽑아주세요
5. 감성을 판별할 수 있는 감성 키워드도 별도로 제공해주세요

JSON 형식:
{{
  "negative_topics": [
    {{"name": "...", "description": "...", "keywords": ["...", "..."]}}
  ],
  "positive_topics": [
    {{"name": "...", "description": "...", "keywords": ["...", "..."]}}
  ],
  "sentiment_signals": {{
    "strong_negative": ["키워드1", "키워드2", ...],
    "mild_negative": ["키워드1", ...],
    "strong_positive": ["키워드1", ...],
    "mild_positive": ["키워드1", ...]
  }}
}}

댓글 샘플:
{sample_text}"""

    log("[1/3] AI가 댓글을 읽고 카테고리를 발견하는 중...")
    raw = call_bedrock(system, prompt, max_tokens=4096)

    # JSON 추출
    match = re.search(r'\{[\s\S]*\}', raw)
    if match:
        categories = json.loads(match.group())
    else:
        raise ValueError(f"AI 응답에서 JSON을 찾을 수 없음: {raw[:500]}")

    log(f"  부정 주제 {len(categories['negative_topics'])}개 발견:")
    for t in categories["negative_topics"]:
        log(f"    - {t['name']}: {t['description']}")
    log(f"  긍정 주제 {len(categories['positive_topics'])}개 발견:")
    for t in categories["positive_topics"]:
        log(f"    - {t['name']}: {t['description']}")

    return categories


def save_categories_csv(categories: dict, path: str):
    """카테고리를 CSV로 저장한다."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["type", "name", "description", "keywords"])
        for t in categories.get("negative_topics", []):
            writer.writerow(["negative", t["name"], t["description"], "|".join(t["keywords"])])
        for t in categories.get("positive_topics", []):
            writer.writerow(["positive", t["name"], t["description"], "|".join(t["keywords"])])


def load_categories_csv(path: str) -> dict:
    """CSV에서 카테고리를 로드한다."""
    categories = {"negative_topics": [], "positive_topics": []}
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            entry = {
                "name": row["name"],
                "description": row["description"],
                "keywords": row["keywords"].split("|") if row["keywords"] else [],
            }
            if row["type"] == "negative":
                categories["negative_topics"].append(entry)
            elif row["type"] == "positive":
                categories["positive_topics"].append(entry)
    return categories


# ============================================================
# 2단계: 전체 댓글을 AI가 분류
# ============================================================

def classify_batch(comments_batch: list, categories: dict) -> list:
    """댓글 배치를 AI에게 보내 분류한다."""
    neg_names = [t["name"] for t in categories["negative_topics"]]
    pos_names = [t["name"] for t in categories["positive_topics"]]

    topic_desc = "부정 주제:\n"
    for t in categories["negative_topics"]:
        topic_desc += f"  - {t['name']}: {t['description']} (키워드: {', '.join(t['keywords'][:8])})\n"
    topic_desc += "긍정 주제:\n"
    for t in categories["positive_topics"]:
        topic_desc += f"  - {t['name']}: {t['description']} (키워드: {', '.join(t['keywords'][:8])})\n"

    comments_text = ""
    for i, c in enumerate(comments_batch):
        comments_text += f"[{i}] {c['text'][:400]}\n"

    system = "You are a sentiment classifier. Respond ONLY with a valid JSON array."

    prompt = f"""아래 댓글들을 분류해주세요.

{topic_desc}

각 댓글에 대해:
- sentiment: "positive", "negative", "mixed", "neutral" 중 하나
- negative_topics: 해당되는 부정 주제 이름 리스트 (없으면 빈 리스트)
- positive_topics: 해당되는 긍정 주제 이름 리스트 (없으면 빈 리스트)

스팸 댓글(외부 사이트 홍보, 무관한 내용)은 sentiment를 "spam"으로 표시하세요.

JSON 배열로 응답 (인덱스 순서대로):
[{{"sentiment":"...","negative_topics":[...],"positive_topics":[...]}}, ...]

댓글:
{comments_text}"""

    raw = call_bedrock(system, prompt, max_tokens=8192)

    match = re.search(r'\[[\s\S]*\]', raw)
    if match:
        results = json.loads(match.group())
    else:
        # 파싱 실패시 전부 neutral 처리
        results = [{"sentiment": "neutral", "negative_topics": [], "positive_topics": []} for _ in comments_batch]

    # 개수 맞추기
    while len(results) < len(comments_batch):
        results.append({"sentiment": "neutral", "negative_topics": [], "positive_topics": []})

    return results[:len(comments_batch)]


def classify_all(comments: list, categories: dict, progress=None) -> list:
    """전체 댓글을 배치로 나누어 분류한다."""
    log = progress if progress else print

    BATCH_SIZE = 50
    all_results = []

    total_batches = (len(comments) + BATCH_SIZE - 1) // BATCH_SIZE
    log(f"[2/3] AI가 전체 {len(comments)}개 댓글 분류 중 ({total_batches}개 배치)...")

    for i in range(0, len(comments), BATCH_SIZE):
        batch = comments[i:i+BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        log(f"  배치 {batch_num}/{total_batches} ({len(batch)}개)...")

        try:
            results = classify_batch(batch, categories)
            all_results.extend(results)
            log(f"  배치 {batch_num}/{total_batches} OK")
        except Exception as e:
            log(f"  배치 {batch_num}/{total_batches} 오류: {e}")
            all_results.extend([{"sentiment": "neutral", "negative_topics": [], "positive_topics": []} for _ in batch])

        # Rate limit
        if batch_num < total_batches:
            time.sleep(0.5)

    return all_results


# ============================================================
# 3단계: 통계 집계 + HTML 대시보드 생성
# ============================================================

def aggregate(comments: list, results: list, video_map: dict) -> dict:
    """분류 결과를 집계한다."""
    stats = {
        "total": 0,
        "positive": 0,
        "negative": 0,
        "mixed": 0,
        "neutral": 0,
        "spam": 0,
        "neg_topics": defaultdict(int),
        "pos_topics": defaultdict(int),
        "neg_examples": defaultdict(list),
        "pos_examples": defaultdict(list),
        "per_video": {},
        "daily": defaultdict(lambda: {"positive": 0, "negative": 0, "mixed": 0, "neutral": 0}),
    }

    for c, r in zip(comments, results):
        sentiment = r.get("sentiment", "neutral")
        if sentiment == "spam":
            stats["spam"] += 1
            continue

        stats["total"] += 1
        if sentiment in stats:
            stats[sentiment] += 1
        else:
            stats["neutral"] += 1

        vid = c.get("video_id", "")
        if vid not in stats["per_video"]:
            stats["per_video"][vid] = {
                "title": video_map.get(vid, vid),
                "positive": 0, "negative": 0, "mixed": 0, "neutral": 0,
            }
        pv = stats["per_video"][vid]
        if sentiment in pv:
            pv[sentiment] += 1

        date_str = c.get("published", "")[:10]
        if date_str:
            stats["daily"][date_str][sentiment] = stats["daily"][date_str].get(sentiment, 0) + 1

        likes = c.get("likes", 0)

        for topic in r.get("negative_topics", []):
            if not isinstance(topic, str):
                continue
            stats["neg_topics"][topic] += 1
            stats["neg_examples"][topic].append({"text": c["text"][:250], "likes": likes, "author": c.get("author", "")})

        for topic in r.get("positive_topics", []):
            if not isinstance(topic, str):
                continue
            stats["pos_topics"][topic] += 1
            stats["pos_examples"][topic].append({"text": c["text"][:250], "likes": likes, "author": c.get("author", "")})

    # 전체 예시를 좋아요 순으로 정렬
    for exs in stats["neg_examples"].values():
        exs.sort(key=lambda x: x["likes"], reverse=True)
    for exs in stats["pos_examples"].values():
        exs.sort(key=lambda x: x["likes"], reverse=True)

    return stats


def generate_action_items(categories: dict, stats: dict) -> list:
    """부정 주제별 액션 아이템을 생성한다 (API 호출 없음)."""
    neg_sorted = sorted(stats["neg_topics"].items(), key=lambda x: x[1], reverse=True)
    cat_map = {t["name"]: t for t in categories.get("negative_topics", [])}

    items = []
    for topic_name, count in neg_sorted:
        cat = cat_map.get(topic_name, {})
        keywords = cat.get("keywords", [])
        description = cat.get("description", "")

        # 좋아요 상위 5개 근거 댓글
        refs = stats["neg_examples"].get(topic_name, [])[:5]

        # 토픽 정보 기반 제안 생성
        suggestion = f"{description} 관련 사용자 피드백을 반영하여 개선 필요."
        if keywords:
            suggestion += f" (관련 키워드: {', '.join(keywords[:5])})"

        items.append({
            "topic": topic_name,
            "description": description,
            "suggestion": suggestion,
            "count": count,
            "refs": refs,
        })

    return items


def build_html(stats: dict, categories: dict, action_items: list = None,
               game_name_ko: str = "붉은사막", game_name_en: str = "Crimson Desert") -> str:
    """대시보드 HTML 생성"""
    neg_topics = dict(sorted(stats["neg_topics"].items(), key=lambda x: x[1], reverse=True))
    pos_topics = dict(sorted(stats["pos_topics"].items(), key=lambda x: x[1], reverse=True))

    # 카테고리 설명 맵
    neg_desc = {t["name"]: t["description"] for t in categories["negative_topics"]}
    pos_desc = {t["name"]: t["description"] for t in categories["positive_topics"]}

    # 날짜별 감성 데이터 준비
    daily_sorted = sorted(stats.get("daily", {}).items())
    daily_labels = [d[0] for d in daily_sorted]
    daily_pos = [d[1].get("positive", 0) for d in daily_sorted]
    daily_neg = [d[1].get("negative", 0) for d in daily_sorted]
    daily_mix = [d[1].get("mixed", 0) for d in daily_sorted]
    daily_neu = [d[1].get("neutral", 0) for d in daily_sorted]

    video_labels = []
    video_urls = []
    video_pos = []
    video_neg = []
    video_mix = []
    video_neu = []
    for vid_id, v in stats["per_video"].items():
        label = v["title"]
        if len(label) > 25:
            label = label[:25] + "..."
        video_labels.append(label)
        video_urls.append(f"https://www.youtube.com/watch?v={vid_id}")
        video_pos.append(v["positive"])
        video_neg.append(v["negative"])
        video_mix.append(v["mixed"])
        video_neu.append(v["neutral"])

    def esc(s):
        return html_mod.escape(s)

    def js_arr(arr):
        return json.dumps(arr, ensure_ascii=False)

    # 개선사항 HTML (부정 주제 기반, 건수 순) — 펼침 가능한 댓글
    improvements_html = ""
    neg_sorted = sorted(stats["neg_topics"].items(), key=lambda x: x[1], reverse=True)
    for rank, (topic, cnt) in enumerate(neg_sorted):
        desc = neg_desc.get(topic, "")
        all_exs = stats["neg_examples"].get(topic, [])
        visible_exs = all_exs[:3]
        hidden_exs = all_exs[3:]

        examples_html = ""
        for ex in visible_exs:
            examples_html += f'<div class="example">"<em>{esc(ex["text"])}</em>" — {esc(ex["author"])} (👍 {ex["likes"]})</div>'

        if hidden_exs:
            examples_html += f'<div class="hidden-examples" id="neg-examples-{rank}" style="display:none">'
            for ex in hidden_exs:
                examples_html += f'<div class="example">"<em>{esc(ex["text"])}</em>" — {esc(ex["author"])} (👍 {ex["likes"]})</div>'
            examples_html += '</div>'
            examples_html += f'<button class="expand-btn" onclick="toggleExamples(\'neg-examples-{rank}\', this, {len(hidden_exs)})">더보기 ({len(hidden_exs)}건)</button>'

        if rank == 0:
            pri_class, pri_label = "high", "최우선"
        elif rank <= 2:
            pri_class, pri_label = "high", "높음"
        elif cnt >= 30:
            pri_class, pri_label = "mid", "중간"
        else:
            pri_class, pri_label = "low", "관찰"

        improvements_html += f"""
        <div class="improvement-card">
            <div class="imp-header">
                <h3>{esc(topic)}</h3>
                <span class="priority {pri_class}">{pri_label} · {cnt}건</span>
            </div>
            <p>{esc(desc)}</p>
            {examples_html}
        </div>"""

    # 액션 아이템 HTML
    action_items_html = ""
    if action_items:
        for idx, item in enumerate(action_items):
            refs_html = ""
            for ref in item["refs"]:
                refs_html += f'<div class="action-ref">"<em>{esc(ref["text"])}</em>" — {esc(ref["author"])} (👍 {ref["likes"]})</div>'
            action_items_html += f"""
            <div class="action-item">
                <span class="action-num">{idx + 1}</span>
                <div>
                    <strong>{esc(item["topic"])}</strong> <span class="count-badge">{item["count"]}건</span>
                    <p>{esc(item["suggestion"])}</p>
                    {refs_html}
                </div>
            </div>"""

    # 강점 HTML — 펼침 가능한 댓글
    strengths_html = ""
    pos_sorted = sorted(stats["pos_topics"].items(), key=lambda x: x[1], reverse=True)
    for idx, (topic, cnt) in enumerate(pos_sorted):
        desc = pos_desc.get(topic, "")
        all_exs = stats["pos_examples"].get(topic, [])
        visible_exs = all_exs[:3]
        hidden_exs = all_exs[3:]

        examples_html = ""
        for ex in visible_exs:
            examples_html += f'<div class="example">"<em>{esc(ex["text"])}</em>" — {esc(ex["author"])} (👍 {ex["likes"]})</div>'

        if hidden_exs:
            examples_html += f'<div class="hidden-examples" id="pos-examples-{idx}" style="display:none">'
            for ex in hidden_exs:
                examples_html += f'<div class="example">"<em>{esc(ex["text"])}</em>" — {esc(ex["author"])} (👍 {ex["likes"]})</div>'
            examples_html += '</div>'
            examples_html += f'<button class="expand-btn" onclick="toggleExamples(\'pos-examples-{idx}\', this, {len(hidden_exs)})">더보기 ({len(hidden_exs)}건)</button>'

        strengths_html += f"""
        <div class="strength-card">
            <h3>{esc(topic)} <span class="count-badge">{cnt}건</span></h3>
            <p>{esc(desc)}</p>
            {examples_html}
        </div>"""

    total = stats["total"]
    pos_pct = stats["positive"] * 100 // max(total, 1)
    neg_pct = stats["negative"] * 100 // max(total, 1)
    mix_pct = stats["mixed"] * 100 // max(total, 1)
    neu_pct = stats["neutral"] * 100 // max(total, 1)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{game_name_ko} 유튜브 댓글 감성 분석 대시보드</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: 'Pretendard','Apple SD Gothic Neo','Noto Sans KR',sans-serif; background:#0f1117; color:#e0e0e0; }}
.container {{ max-width:1200px; margin:0 auto; padding:24px; }}
h1 {{ text-align:center; font-size:28px; padding:32px 0 8px; color:#fff; }}
.subtitle {{ text-align:center; color:#888; font-size:14px; margin-bottom:32px; }}
.method-note {{ text-align:center; color:#666; font-size:12px; margin-top:-20px; margin-bottom:32px; }}

.cards {{ display:grid; grid-template-columns: repeat(5,1fr); gap:12px; margin-bottom:32px; }}
.card {{ background:#1a1d27; border-radius:12px; padding:20px; text-align:center; }}
.card .number {{ font-size:32px; font-weight:700; }}
.card .label {{ font-size:12px; color:#888; margin-top:4px; }}
.card.pos .number {{ color:#4ade80; }}
.card.neg .number {{ color:#f87171; }}
.card.mix .number {{ color:#c084fc; }}
.card.neu .number {{ color:#facc15; }}
.card.total .number {{ color:#60a5fa; }}

.chart-grid {{ display:grid; grid-template-columns: 1fr 1fr; gap:24px; margin-bottom:32px; }}
.chart-box {{ background:#1a1d27; border-radius:12px; padding:24px; }}
.chart-box h2 {{ font-size:16px; margin-bottom:16px; color:#ccc; }}
.chart-full {{ grid-column: 1 / -1; }}

.section-title {{ font-size:22px; font-weight:700; margin:40px 0 20px; padding-left:12px; border-left:4px solid #f87171; }}
.section-title.pos-title {{ border-left-color:#4ade80; }}
.improvement-card, .strength-card {{ background:#1a1d27; border-radius:12px; padding:20px; margin-bottom:16px; }}
.imp-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
.imp-header h3 {{ font-size:16px; }}
.priority {{ font-size:12px; padding:4px 10px; border-radius:20px; font-weight:600; white-space:nowrap; }}
.priority.high {{ background:#7f1d1d; color:#fca5a5; }}
.priority.mid {{ background:#78350f; color:#fde68a; }}
.priority.low {{ background:#14532d; color:#86efac; }}
.imp-stat {{ font-size:13px; color:#888; margin:8px 0; }}
.example {{ font-size:13px; color:#aaa; margin:6px 0; padding:8px 12px; background:#12141c; border-radius:8px; line-height:1.6; }}
.strength-card h3 {{ font-size:16px; color:#4ade80; margin-bottom:6px; }}
.strength-card p {{ font-size:14px; color:#bbb; }}
.count-badge {{ font-size:12px; color:#888; font-weight:400; }}

.expand-btn {{ display:block; margin:10px 0 0; padding:6px 14px; background:#2a2d3a; color:#aaa; border:none; border-radius:8px; font-size:13px; cursor:pointer; transition:background 0.2s; }}
.expand-btn:hover {{ background:#3a3d4a; color:#fff; }}

.improvements-grid {{ display:grid; grid-template-columns: 1fr 1fr; gap:24px; }}
.improvements-left {{ min-width:0; }}
.improvements-right {{ min-width:0; }}
.action-panel {{ position:sticky; top:24px; background:#1a1d27; border-radius:12px; padding:20px; }}
.action-panel h3 {{ font-size:18px; color:#f87171; margin-bottom:16px; border-bottom:1px solid #2a2d3a; padding-bottom:10px; }}
.action-item {{ display:flex; gap:12px; margin-bottom:18px; padding-bottom:18px; border-bottom:1px solid #1e2130; }}
.action-item:last-child {{ border-bottom:none; margin-bottom:0; padding-bottom:0; }}
.action-num {{ flex-shrink:0; width:28px; height:28px; background:#7f1d1d; color:#fca5a5; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:13px; font-weight:700; margin-top:2px; }}
.action-item strong {{ font-size:14px; color:#e0e0e0; }}
.action-item p {{ font-size:13px; color:#aaa; margin:6px 0; line-height:1.6; }}
.action-ref {{ font-size:12px; color:#777; margin:4px 0; padding:6px 10px; background:#12141c; border-radius:6px; line-height:1.5; }}

#videoStack {{ cursor:pointer; }}

@media (max-width:768px) {{
    .cards {{ grid-template-columns: repeat(2,1fr); }}
    .chart-grid {{ grid-template-columns: 1fr; }}
    .improvements-grid {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<div class="container">
    <h1>{game_name_ko} 유튜브 댓글 감성 분석</h1>
    <p class="subtitle">{game_name_en} YouTube Comment Sentiment Dashboard</p>
    <p class="method-note">AI(Claude via Bedrock)가 댓글을 직접 읽고 주제를 발견 · 분류한 결과 — 스팸 {stats['spam']}건 제외, 총 {total}개 분석</p>

    <div class="cards">
        <div class="card total"><div class="number">{total}</div><div class="label">분석 댓글</div></div>
        <div class="card pos"><div class="number">{stats['positive']}<small style="font-size:14px;color:#888"> ({pos_pct}%)</small></div><div class="label">긍정</div></div>
        <div class="card neg"><div class="number">{stats['negative']}<small style="font-size:14px;color:#888"> ({neg_pct}%)</small></div><div class="label">부정</div></div>
        <div class="card mix"><div class="number">{stats['mixed']}<small style="font-size:14px;color:#888"> ({mix_pct}%)</small></div><div class="label">혼합 (긍부정)</div></div>
        <div class="card neu"><div class="number">{stats['neutral']}<small style="font-size:14px;color:#888"> ({neu_pct}%)</small></div><div class="label">중립</div></div>
    </div>

    <div class="chart-grid">
        <div class="chart-box">
            <h2>전체 감성 분포</h2>
            <canvas id="sentimentDonut"></canvas>
        </div>
        <div class="chart-box">
            <h2>부정 주제 (AI 발견)</h2>
            <canvas id="negBar"></canvas>
        </div>
        <div class="chart-box">
            <h2>긍정 주제 (AI 발견)</h2>
            <canvas id="posBar"></canvas>
        </div>
        <div class="chart-box">
            <h2>감성 비율 비교</h2>
            <canvas id="ratioBar"></canvas>
        </div>
        <div class="chart-box chart-full">
            <h2>날짜별 감성 추이</h2>
            <canvas id="dailyTrend" height="80"></canvas>
        </div>
        <div class="chart-box chart-full">
            <h2>영상별 감성 비교</h2>
            <canvas id="videoStack" height="100"></canvas>
        </div>
    </div>

    <div class="section-title">개선사항 — 부정 댓글에서 AI가 발견한 주제</div>
    <div class="improvements-grid">
        <div class="improvements-left">
            {improvements_html}
        </div>
        <div class="improvements-right">
            <div class="action-panel">
                <h3>액션 아이템</h3>
                {action_items_html}
            </div>
        </div>
    </div>

    <div class="section-title pos-title">강점 — 긍정 댓글에서 AI가 발견한 주제</div>
    {strengths_html}
</div>

<script>
function toggleExamples(id, btn, hiddenCount) {{
    var el = document.getElementById(id);
    if (el.style.display === 'none') {{
        el.style.display = 'block';
        btn.textContent = '접기';
    }} else {{
        el.style.display = 'none';
        btn.textContent = '더보기 (' + hiddenCount + '건)';
    }}
}}

Chart.defaults.color = '#aaa';
Chart.defaults.borderColor = '#2a2d3a';

new Chart(document.getElementById('sentimentDonut'), {{
    type: 'doughnut',
    data: {{
        labels: ['긍정','부정','혼합','중립'],
        datasets: [{{ data: [{stats['positive']},{stats['negative']},{stats['mixed']},{stats['neutral']}],
                      backgroundColor: ['#4ade80','#f87171','#c084fc','#facc15'], borderWidth:0 }}]
    }},
    options: {{ plugins: {{ legend: {{ position:'bottom' }} }} }}
}});

new Chart(document.getElementById('negBar'), {{
    type: 'bar',
    data: {{
        labels: {js_arr(list(neg_topics.keys()))},
        datasets: [{{ data: {json.dumps(list(neg_topics.values()))}, backgroundColor:'#f87171', borderRadius:6 }}]
    }},
    options: {{ indexAxis:'y', plugins:{{ legend:{{ display:false }} }}, scales:{{ x:{{ beginAtZero:true }} }} }}
}});

new Chart(document.getElementById('posBar'), {{
    type: 'bar',
    data: {{
        labels: {js_arr(list(pos_topics.keys()))},
        datasets: [{{ data: {json.dumps(list(pos_topics.values()))}, backgroundColor:'#4ade80', borderRadius:6 }}]
    }},
    options: {{ indexAxis:'y', plugins:{{ legend:{{ display:false }} }}, scales:{{ x:{{ beginAtZero:true }} }} }}
}});

new Chart(document.getElementById('ratioBar'), {{
    type: 'bar',
    data: {{
        labels: ['긍정','부정','혼합','중립'],
        datasets: [{{ data: [{stats['positive']},{stats['negative']},{stats['mixed']},{stats['neutral']}],
                      backgroundColor:['#4ade80','#f87171','#c084fc','#facc15'], borderRadius:6 }}]
    }},
    options: {{ plugins:{{ legend:{{ display:false }} }}, scales:{{ y:{{ beginAtZero:true }} }} }}
}});

new Chart(document.getElementById('dailyTrend'), {{
    type: 'line',
    data: {{
        labels: {js_arr(daily_labels)},
        datasets: [
            {{ label:'긍정', data:{json.dumps(daily_pos)}, borderColor:'#4ade80', backgroundColor:'rgba(74,222,128,0.1)', fill:true, tension:0.3, pointRadius:2 }},
            {{ label:'부정', data:{json.dumps(daily_neg)}, borderColor:'#f87171', backgroundColor:'rgba(248,113,113,0.1)', fill:true, tension:0.3, pointRadius:2 }},
            {{ label:'혼합', data:{json.dumps(daily_mix)}, borderColor:'#c084fc', backgroundColor:'rgba(192,132,252,0.1)', fill:true, tension:0.3, pointRadius:2 }},
            {{ label:'중립', data:{json.dumps(daily_neu)}, borderColor:'#facc15', backgroundColor:'rgba(250,204,21,0.1)', fill:true, tension:0.3, pointRadius:2 }},
        ]
    }},
    options: {{ scales:{{ x:{{ ticks:{{ maxRotation:45 }} }}, y:{{ beginAtZero:true }} }}, plugins:{{ legend:{{ position:'top' }} }} }}
}});

var videoUrls = {js_arr(video_urls)};
var videoStackChart = new Chart(document.getElementById('videoStack'), {{
    type: 'bar',
    data: {{
        labels: {js_arr(video_labels)},
        datasets: [
            {{ label:'긍정', data:{json.dumps(video_pos)}, backgroundColor:'#4ade80' }},
            {{ label:'부정', data:{json.dumps(video_neg)}, backgroundColor:'#f87171' }},
            {{ label:'혼합', data:{json.dumps(video_mix)}, backgroundColor:'#c084fc' }},
            {{ label:'중립', data:{json.dumps(video_neu)}, backgroundColor:'#facc15' }},
        ]
    }},
    options: {{
        scales:{{ x:{{ stacked:true }}, y:{{ stacked:true, beginAtZero:true }} }},
        plugins:{{ legend:{{ position:'top' }} }},
        onClick: function(evt) {{
            var points = videoStackChart.getElementsAtEventForMode(evt, 'index', {{ intersect: false }});
            if (points.length > 0) {{
                var idx = points[0].index;
                window.open(videoUrls[idx], '_blank');
            }}
        }}
    }}
}});
</script>
</body>
</html>"""


# ============================================================
# 핵심 로직 — 외부에서 호출 가능
# ============================================================

def analyze(game_name_ko: str = "붉은사막", game_name_en: str = "Crimson Desert",
            developer: str = "Pearl Abyss", input_file: str = "comments_output.json",
            output_dir: str = ".", test_mode: bool = False,
            progress=None) -> str:
    """
    분석 파이프라인 실행.

    Parameters:
        game_name_ko: 게임 한국어명
        game_name_en: 게임 영어명
        developer: 개발사명
        input_file: 댓글 JSON 파일 경로
        output_dir: 출력 파일 저장 디렉토리
        test_mode: True이면 댓글 200개로 제한
        progress: (message: str) → None 콜백 (없으면 print)

    Returns:
        str: 생성된 dashboard.html 파일 경로
    """
    log = progress if progress else print

    os.makedirs(output_dir, exist_ok=True)

    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 모든 댓글을 플랫 리스트로
    comments = []
    video_map = {}
    for vid_id, vid_data in data.items():
        video_map[vid_id] = vid_data["title"]
        for c in vid_data["comments"]:
            comments.append({
                "text": c["text"], "likes": c.get("likes", 0),
                "author": c["author"], "video_id": vid_id,
                "published": c.get("published", ""),
            })
            for r in c.get("replies", []):
                comments.append({
                    "text": r["text"], "likes": r.get("likes", 0),
                    "author": r["author"], "video_id": vid_id,
                    "published": r.get("published", ""),
                })

    if test_mode:
        comments = comments[:200]
        log("[테스트 모드] 댓글을 200개로 제한")

    log(f"총 {len(comments)}개 댓글 로드")

    # 1단계: 카테고리 발견 (CSV 캐시 우선)
    categories_cache_path = os.path.join(output_dir, "categories_cache.csv")
    if os.path.exists(categories_cache_path):
        log(f"[1/3] 캐시에서 카테고리 로드: {categories_cache_path}")
        categories = load_categories_csv(categories_cache_path)
        log(f"  부정 주제 {len(categories['negative_topics'])}개, 긍정 주제 {len(categories['positive_topics'])}개 로드")
    else:
        categories = discover_categories(comments, game_name_ko, game_name_en, developer, progress=log)
        save_categories_csv(categories, categories_cache_path)
        log(f"  → {categories_cache_path} 저장")

        # 카테고리 저장 (디버깅용 JSON)
        cat_json_path = os.path.join(output_dir, "categories_discovered.json")
        with open(cat_json_path, "w", encoding="utf-8") as f:
            json.dump(categories, f, ensure_ascii=False, indent=2)
        log(f"  → categories_discovered.json 저장")

    # 2단계: 전체 분류
    results = classify_all(comments, categories, progress=log)

    # 분류 결과 저장 (JSON)
    cls_json_path = os.path.join(output_dir, "classification_results.json")
    with open(cls_json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log("  → classification_results.json 저장")

    # 분류 결과 저장 (CSV)
    cls_csv_path = os.path.join(output_dir, "classification_output.csv")
    with open(cls_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["video_id", "video_title", "author", "text", "likes", "published",
                         "sentiment", "negative_topics", "positive_topics"])
        for c, r in zip(comments, results):
            writer.writerow([
                c.get("video_id", ""),
                video_map.get(c.get("video_id", ""), ""),
                c.get("author", ""),
                c.get("text", ""),
                c.get("likes", 0),
                c.get("published", ""),
                r.get("sentiment", "neutral"),
                "|".join(r.get("negative_topics", [])),
                "|".join(r.get("positive_topics", [])),
            ])
    log("  → classification_output.csv 저장")

    # 3단계: 집계 + 대시보드
    log("[3/3] 대시보드 생성 중...")
    stats = aggregate(comments, results, video_map)
    action_items = generate_action_items(categories, stats)

    log(f"분석 결과: 총 {stats['total']}개 (스팸 {stats['spam']}개 제외)")
    log(f"  긍정 {stats['positive']}개 / 부정 {stats['negative']}개 / 혼합 {stats['mixed']}개 / 중립 {stats['neutral']}개")

    html_content = build_html(stats, categories, action_items,
                              game_name_ko=game_name_ko, game_name_en=game_name_en)
    dashboard_path = os.path.join(output_dir, "dashboard.html")
    with open(dashboard_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    log(f"dashboard.html 저장 완료: {dashboard_path}")

    return dashboard_path


# ============================================================
# CLI 메인
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="유튜브 댓글 감성 분석 대시보드 생성")
    parser.add_argument("--game-ko", default="붉은사막", help="게임 한국어명 (기본: 붉은사막)")
    parser.add_argument("--game-en", default="Crimson Desert", help="게임 영어명 (기본: Crimson Desert)")
    parser.add_argument("--developer", default="Pearl Abyss", help="개발사명 (기본: Pearl Abyss)")
    parser.add_argument("--input", default="comments_output.json", help="입력 JSON 파일 경로")
    parser.add_argument("--output-dir", default=".", help="출력 디렉토리")
    parser.add_argument("--test", action="store_true", help="테스트 모드 (200개 댓글 제한)")
    args = parser.parse_args()

    analyze(
        game_name_ko=args.game_ko,
        game_name_en=args.game_en,
        developer=args.developer,
        input_file=args.input,
        output_dir=args.output_dir,
        test_mode=args.test,
    )


if __name__ == "__main__":
    main()
