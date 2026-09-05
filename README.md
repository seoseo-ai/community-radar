# Community Radar (커뮤니티 레이더)

실시간 커뮤니티 신호 수집기. Reddit, DC Inside, Hacker News, GitHub, YouTube, Discord에서 핫 토픽/이슈를 통합 수집합니다.

## 소스별 수집기

| 스크립트 | 소스 | 기능 |
|----------|------|------|
| `reddit_fetch.py` | Reddit | 서브레딧 hot/search, 포스트/댓글 조회 |
| `dc_fetch.py` | DC Inside | 갤러리 목록, 실베, 검색, 포스트/댓글 조회 |
| `hn_fetch.py` | Hacker News | 스토리/댓글 검색, top/new stories |
| `github_fetch.py` | GitHub | 이슈/PR 검색, 리포지토리 검색 |
| `youtube_fetch.py` | YouTube | 영상 검색 |
| `discord_fetch.py` | Discord | 채널 메시지 수집 |
| `community_aggregate.py` | 전체 | 통합 래퍼 — 모든 소스에서 수집 → 정렬 → 버킷 분류 |

## 사용법

```bash
# 기본: 전체 소스에서 핫 토픽 수집
python3 community_aggregate.py hot

# 특정 소스만
python3 community_aggregate.py hot --sources reddit dc hn

# 검색어로 특정 주제 수집
python3 community_aggregate.py "OpenClaw"

# 개별 수집기 직접 사용
python3 dc_fetch.py best --limit 10
python3 reddit_fetch.py subreddit technology --sort hot --limit 5
python3 hn_fetch.py search "AI" --limit 10
python3 github_fetch.py search-issues "label:bug" --limit 5
```

### 프리셋

```bash
python3 community_aggregate.py --preset general-news   # 일반 뉴스
python3 community_aggregate.py --preset signal-news    # 저품질 밈/잡담 억제 + 의미 신호 우선
python3 community_aggregate.py --preset agent-news      # 에이전트/AI 관련
```

### 신호 품질 모드

```bash
# 기본: raw 인기도 + 품질 점수를 함께 반영
python3 community_aggregate.py hot --quality-mode balanced

# 의미 있는 뉴스/개발/정책/보안/AI 신호만 더 강하게 선별
python3 community_aggregate.py --preset signal-news

# 직접 임계값 지정: signal score 50점 이상만
python3 community_aggregate.py hot --quality-mode signal --min-quality 50
```

각 항목에는 `signal: 점수/등급 (이유)`가 붙습니다. 예: `topic:openai,model`, `source:hn`, `strong_reaction`.

## 의존성

- Python 3.10+
- 표준 라이브러리만 사용 (urllib, json, html.parser)
- Discord 수집 시 Discord API 토큰 필요 (`DISCORD_TOKEN` 환경변수)

## 라이선스

MIT

## Offline CI

Pull requests and main-branch pushes compile tracked Python and check Bash syntax without importing or executing operational entry points. No deployment or service restart is performed by this workflow.
