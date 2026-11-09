# feedback-tool — 파일 안내

사용법은 **`작업/README.md`** 를 보세요. 여기는 각 파일이 하는 일만 정리했어요.
모두 python3 표준 라이브러리만 써요 (pip·npm 설치 없음). ffprobe·xmllint가 있으면 더 정확히 확인하고, 없어도 동작해요.

| 파일 | 하는 일 |
|---|---|
| `피드백도구.command` | 더블클릭 → 검토 페이지 서버를 켜고 브라우저를 열어요 |
| `server.py` | 검토 페이지 서버 (127.0.0.1 전용). 영상 재생, 피드백 저장·수정·삭제, 화면 캡처 |
| `index.html` | 검토 페이지 화면 |
| `fb.py` | Claude Code가 쓰는 명령: `status`, `brief`(작업 지시서), `done`, `question`, `skip`, `reopen`, `publish`, `open`, `stop`, `sync`(고객 피드백) |
| `fcpxml_edit.py` | FCPXML 수정 엔진: `info`, `locate`, `apply`(계획대로 새 버전 저장), `validate` |
| `fcpxml.py` | FCPXML 읽기 (시점 → 클립·자막 찾기) |
| `claude_job.py` | 검토 페이지의 "반영 맡기기" 버튼 → 화면 없는 Claude Code 실행·진행 표시 |
| `remote.py` | 구글 드라이브 영상 링크(`links.json`), 고객 피드백 연결·고객 링크·동기화 |
| `google/Code.gs` | 고객용 페이지 + 동기화 API (구글 Apps Script에 붙여넣는 코드 — `server.gs`·`client.html`을 `build.py`로 합친 것) |
| `selftest.py` | 타임라인 사본으로 전체 과정 자체 테스트 (원본 안 바뀜) |
| `claude_setup/` | Claude Code 설정 원본 (`/apply-feedback` 명령, 스킬, 권한, 훅). `시작하기.command`가 `.claude/`에 설치해요 |
| `start.command` | 문제 확인용: 터미널 창에서 서버를 직접 켜기 (`Ctrl + C`로 끄기) |

**피드백 파일** `작업/feedback/<영상이름>.feedback.json`
```json
{
  "video": "컨펌본/intro_v2.mp4", "fcpxml": "타임라인/intro_v2.fcpxml",
  "fps": "30000/1001", "timeline_tc_start": "01:00:00:00",
  "items": [{
    "id": "f001", "start_tc": "00:00:12:15", "end_tc": "00:00:14:02",
    "start_seconds": 12.512, "end_seconds": 14.081,
    "start_frame": 375, "end_frame": 422,
    "timeline_start_tc": "01:00:12:15", "timeline_end_tc": "01:00:14:02",
    "type": "컷 삭제", "memo": "여기 말 더듬는 부분 잘라줘",
    "status": "todo", "claude_note": "", "resolved_in": null
  }]
}
```
- `status`: `todo`(대기) · `done`(반영) · `question`(질문) · `skipped`(보류)
- 시간은 영상 기준(시작 = `00:00:00:00`), `timeline_*` 는 리졸브 타임라인 위치. 프레임 번호가 정확한 기준이에요.
- 예전 형식(`<영상이름>.json`)은 처음 열 때 자동으로 바뀌고, 옛 파일은 `.json.migrated` 로 남아요.
- 고객 피드백 항목에는 `origin: "client"`, `author`(고객 이름), `remote_id`(시트의 id)가 더 붙어요.
