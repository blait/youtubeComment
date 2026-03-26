# YouTube Comment Analyzer

<img width="903" height="942" alt="image" src="https://github.com/user-attachments/assets/8ca9da5a-fb29-4e84-9fd6-31baf7112f2a" />
<img width="890" height="663" alt="image" src="https://github.com/user-attachments/assets/99aa46ae-33b0-422f-997d-223438860ecb" />
<img width="897" height="952" alt="image" src="https://github.com/user-attachments/assets/01bcad73-392e-464a-85b2-2c147dddd383" />


게임 유튜브 댓글을 자동 수집하고 AI(Claude via AWS Bedrock)로 감성 분석하여 인터랙티브 대시보드를 생성하는 파이프라인입니다.

브라우저에서 게임명과 검색어를 입력하면 댓글 수집 → 카테고리 발견 → 감성 분류 → 대시보드 생성까지 자동으로 실행됩니다.

![Dashboard Preview](https://img.shields.io/badge/Python-3.10+-blue) ![Flask](https://img.shields.io/badge/Flask-Web_UI-green) ![Bedrock](https://img.shields.io/badge/AWS_Bedrock-Claude-orange)

## 주요 기능

- **유튜브 댓글 자동 수집** — 검색어 기반 영상 탐색 + 댓글/답글 수집 (YouTube Data API v3)
- **AI 카테고리 발견** — 인기 댓글 샘플을 Claude에게 보내 긍정/부정 주제를 자동 발견
- **AI 감성 분류** — 전체 댓글을 배치로 분류 (긍정/부정/혼합/중립/스팸)
- **인터랙티브 대시보드** — Chart.js 기반 감성 분포, 주제별 통계, 영상별 비교, 날짜별 추이
- **멀티 게임 지원** — 여러 게임을 독립적으로 분석, 결과를 별도 디렉토리에 저장
- **실시간 진행 상황** — SSE로 브라우저에서 파이프라인 진행 로그 실시간 확인

## 사전 요구사항

- Python 3.10+
- [YouTube Data API v3 키](https://console.cloud.google.com/) (댓글 수집용)
- AWS 자격 증명 (Bedrock Claude 접근용, 감성 분석용)

## 설치

```bash
git clone https://github.com/blait/youtubeComment.git
cd youtubeComment

python3 -m venv .venv
source .venv/bin/activate
pip install flask boto3
```

## 실행

### 방법 1: 웹 UI (권장)

```bash
export YOUTUBE_API_KEY="your_youtube_api_key"
# AWS 자격 증명이 ~/.aws/credentials 에 설정되어 있어야 합니다

python app.py
```

http://localhost:8080 접속 후:

1. 게임명 (한국어/영어), 개발사, 검색어 입력
2. [분석 시작] 클릭
3. 실시간 로그로 진행 상황 확인
4. 완료 시 "대시보드 보기" 링크 클릭

### 방법 2: CLI

```bash
export YOUTUBE_API_KEY="your_youtube_api_key"

# 1단계: 댓글 수집
python youtube_comments.py "붉은사막 리뷰" "붉은사막 후기" "Crimson Desert review"

# 2단계: 감성 분석 + 대시보드 생성
python build_dashboard.py

# 다른 게임 분석 시
python build_dashboard.py --game-ko "엘든 링" --game-en "Elden Ring" --developer "FromSoftware"

# 테스트 모드 (200개 댓글 제한)
python build_dashboard.py --test
```

## 프로젝트 구조

```
youtubeComment/
├── app.py                  # Flask 웹 서버
├── youtube_comments.py     # 유튜브 댓글 수집 스크립트
├── build_dashboard.py      # AI 감성 분석 + 대시보드 생성
├── templates/
│   └── index.html          # 웹 UI (입력 폼 + 진행 상황 + 게임 목록)
└── output/                 # 게임별 분석 결과 (자동 생성)
    └── crimson_desert/
        ├── comments_output.json
        ├── categories_cache.csv
        ├── classification_results.json
        ├── dashboard.html
        └── meta.json
```

## API 비용 참고

| API | 비용 |
|-----|------|
| YouTube Data API v3 | 무료 10,000 units/일 (`search.list` = 100 units, `commentThreads.list` = 1 unit) |
| AWS Bedrock (Claude) | 카테고리 발견 1회 + 분류 N회 (N = 총 댓글 수 / 50) |

## 라이선스

MIT
