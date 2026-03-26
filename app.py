"""
유튜브 댓글 수집 + 감성 분석 Flask 웹 서버
==========================================
브라우저에서 게임명/검색어를 입력하면 수집→분석→대시보드까지 자동 실행.

실행: pip install flask && python app.py
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime
from queue import Queue, Empty

from flask import Flask, render_template, request, jsonify, Response, send_file

import youtube_comments
import build_dashboard

app = Flask(__name__)

OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# 잡 관리
jobs = {}  # {job_id: {status, game_id, queue, result, error}}


def slugify(name: str) -> str:
    """영문명을 소문자 + 언더스코어로 변환."""
    s = name.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    s = s.strip('_')
    return s or "unknown"


def run_pipeline(job_id: str, game_name_ko: str, game_name_en: str,
                 developer: str, queries: list, test_mode: bool):
    """백그라운드에서 수집 + 분석 파이프라인 실행."""
    job = jobs[job_id]
    q = job["queue"]
    game_id = slugify(game_name_en)
    job["game_id"] = game_id
    output_dir = os.path.join(OUTPUT_ROOT, game_id)

    def progress(msg):
        q.put(msg)

    try:
        # 1단계: 댓글 수집
        progress(f"=== 댓글 수집 시작: {game_name_ko} ({game_name_en}) ===")
        max_comments = 200 if test_mode else 100
        results = youtube_comments.collect(
            queries=queries,
            output_dir=output_dir,
            max_results=10,
            max_comments=max_comments,
            comment_order="relevance",
            progress=progress,
        )

        if not results:
            progress("검색 결과가 없습니다. 검색어를 확인해주세요.")
            job["status"] = "failed"
            job["error"] = "검색 결과 없음"
            q.put(None)  # sentinel
            return

        # 2단계: 감성 분석
        progress(f"\n=== 감성 분석 시작 ===")
        input_file = os.path.join(output_dir, "comments_output.json")
        dashboard_path = build_dashboard.analyze(
            game_name_ko=game_name_ko,
            game_name_en=game_name_en,
            developer=developer,
            input_file=input_file,
            output_dir=output_dir,
            test_mode=test_mode,
            progress=progress,
        )

        # 메타데이터 저장
        meta = {
            "game_name_ko": game_name_ko,
            "game_name_en": game_name_en,
            "developer": developer,
            "queries": queries,
            "analyzed_at": datetime.now().isoformat(),
            "test_mode": test_mode,
        }
        with open(os.path.join(output_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        progress(f"\n=== 완료! 대시보드가 생성되었습니다. ===")
        job["status"] = "completed"
        job["result"] = dashboard_path

    except Exception as e:
        progress(f"\n오류 발생: {e}")
        job["status"] = "failed"
        job["error"] = str(e)

    q.put(None)  # sentinel — 스트림 종료 신호


# ============================================================
# 라우트
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    game_name_ko = data.get("game_name_ko", "").strip()
    game_name_en = data.get("game_name_en", "").strip()
    developer = data.get("developer", "").strip()
    queries_raw = data.get("queries", "").strip()
    test_mode = data.get("test_mode", False)

    if not game_name_ko or not game_name_en:
        return jsonify({"error": "게임 한국어명과 영어명은 필수입니다."}), 400
    if not queries_raw:
        return jsonify({"error": "검색어를 입력해주세요."}), 400

    queries = [q.strip() for q in queries_raw.split("\n") if q.strip()]

    job_id = str(uuid.uuid4())[:8]
    game_id = slugify(game_name_en)

    jobs[job_id] = {
        "status": "running",
        "game_id": game_id,
        "queue": Queue(),
        "result": None,
        "error": None,
    }

    t = threading.Thread(
        target=run_pipeline,
        args=(job_id, game_name_ko, game_name_en, developer, queries, test_mode),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id, "game_id": game_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    job = jobs[job_id]
    q = job["queue"]

    def generate():
        while True:
            try:
                msg = q.get(timeout=15)
            except Empty:
                # 큐에 메시지 없어도 keepalive 전송 (SSE 연결 유지)
                if job["status"] == "running":
                    yield f": keepalive\n\n"
                    continue
                else:
                    # 잡이 끝났는데 sentinel을 못 받은 경우
                    result_data = {
                        "status": job["status"],
                        "game_id": job["game_id"],
                    }
                    if job["error"]:
                        result_data["error"] = job["error"]
                    yield f"event: done\ndata: {json.dumps(result_data, ensure_ascii=False)}\n\n"
                    break

            if msg is None:
                # 스트림 종료
                result_data = {
                    "status": job["status"],
                    "game_id": job["game_id"],
                }
                if job["error"]:
                    result_data["error"] = job["error"]
                yield f"event: done\ndata: {json.dumps(result_data, ensure_ascii=False)}\n\n"
                break

            yield f"data: {json.dumps({'message': msg}, ensure_ascii=False)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/games")
def api_games():
    games = []
    if not os.path.isdir(OUTPUT_ROOT):
        return jsonify(games)

    for name in sorted(os.listdir(OUTPUT_ROOT)):
        game_dir = os.path.join(OUTPUT_ROOT, name)
        if not os.path.isdir(game_dir):
            continue

        meta_path = os.path.join(game_dir, "meta.json")
        dashboard_path = os.path.join(game_dir, "dashboard.html")

        if not os.path.exists(dashboard_path):
            continue

        meta = {}
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

        games.append({
            "game_id": name,
            "game_name_ko": meta.get("game_name_ko", name),
            "game_name_en": meta.get("game_name_en", name),
            "developer": meta.get("developer", ""),
            "analyzed_at": meta.get("analyzed_at", ""),
            "has_dashboard": True,
        })

    return jsonify(games)


@app.route("/dashboard/<game_id>")
def dashboard(game_id):
    # game_id 검증 (경로 순회 방지)
    safe_id = re.sub(r'[^a-z0-9_]', '', game_id)
    dashboard_path = os.path.join(OUTPUT_ROOT, safe_id, "dashboard.html")
    if not os.path.exists(dashboard_path):
        return "Dashboard not found", 404
    return send_file(dashboard_path)


if __name__ == "__main__":
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    print(f"Output directory: {OUTPUT_ROOT}")
    print(f"Starting server at http://localhost:8080")
    app.run(debug=True, port=8080)
