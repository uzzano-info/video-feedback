# 영상 피드백 도구 (video-feedback)

컨펌본을 보며 **타임코드별 피드백**을 남기면, Claude Code가 읽고 **다빈치 리졸브 타임라인(FCPXML) 새 버전**을 만들어 주는 도구예요.
고객사는 링크 하나로 들어와 피드백을 남길 수 있어요.

```
고객사 ──(고객 페이지: GitHub Pages)──▶ 구글 시트 (Apps Script)
                                              │  20초마다 자동으로 가져오기
내 맥 ──(검토 페이지: 127.0.0.1)◀─────────────┘
   └─ 🤖 Claude Code가 반영 → 타임라인/이름_v3.fcpxml → 리졸브로 가져오기
```

| 부분 | 어디서 돌아가요 | 하는 일 |
|---|---|---|
| 검토 페이지 · Claude Code 연동 | **내 맥** (`AI영상 편집/작업/feedback-tool`) | 영상 재생, 피드백, 반영 맡기기, FCPXML 수정 |
| 고객 페이지 | **GitHub Pages** (`docs/`) | 고객이 영상을 보며 피드백 남기기 (로그인 없음) |
| 피드백 저장소 | **내 구글 계정** (Apps Script + 시트) | 고객 피드백 저장, 내 맥과 주고받기 |

## 설치 (맥)
1. 이 저장소를 내려받아(Code ▸ Download ZIP) `AI영상 편집` 폴더를 `~/Movies/` 에 둬요.
2. `AI영상 편집/시작하기.command` 더블클릭 — 검토 페이지가 열리고 Claude Code 설정이 설치돼요.
   (python3만 있으면 돼요. pip·npm 설치 없음)

자세한 사용법은 [`AI영상 편집/작업/README.md`](AI영상%20편집/작업/README.md) 를 보세요.

## 고객 페이지 (GitHub Pages)
- 주소: `https://uzzano-info.github.io/video-feedback/?p=<고객 링크 키>&s=<Apps Script 배포 ID>`
- 고객 링크는 검토 페이지의 **🌐 고객 링크** 에서 만들어요. 주소는 직접 만들 필요 없어요.
- `docs/` 는 `AI영상 편집/작업/feedback-tool/google/build.py` 로 만든 파일이에요 (`client.html` 과 같은 화면).
- `docs/config.js` 에 Apps Script 배포 ID를 넣으면 링크에서 `&s=…` 를 빼도 돼요.

## 개인 정보
영상·피드백·연결 정보(`작업/feedback/`, `links.json`)는 저장소에 올리지 않아요 (`.gitignore`).
고객 페이지에는 피드백 내용이 담기지 않고, 링크 키를 아는 사람만 그 영상의 피드백을 볼 수 있어요.
