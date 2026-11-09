# AI영상 편집 — 영상 제작·검토 작업 공간

사용자가 검토 페이지에서 영상을 보며 타임코드별 피드백을 남기면, Claude Code가 그 피드백을 반영해 다음 버전을 만드는 곳이다. Claude Code는 이 폴더(예: `~/Movies/AI영상 편집`)에서 실행한다. 경로에 공백이 있으니 셸 명령의 경로는 항상 따옴표로 감싼다.

## 두 가지 흐름
- **다빈치 리졸브**: 리졸브에서 `컨펌본/<이름>.mp4`로 렌더하고, 같은 이름으로 `타임라인/<이름>.fcpxml`을 내보낸다 → 피드백 → Claude Code가 **수정한 FCPXML 새 버전**을 만든다 → 사용자가 리졸브로 가져와 렌더한다.
- **Claude Code 코드 렌더**: Claude Code가 만든 영상을 렌더한 뒤 **/fb-publish**로 `컨펌본/<이름>_vN.mp4`에 올린다 → 피드백 → Claude Code가 코드를 고치고 다시 렌더해 다음 버전을 올린다.

"피드백 반영해줘" / **/apply-feedback** / 검토 페이지의 "Claude Code에게 반영 맡기기" → `작업/CLAUDE.md`의 절차를 따른다.

## 폴더
| 폴더 | 내용 | 다루는 법 |
|---|---|---|
| `컨펌본/` | 검토용 영상 | 읽기만 (코드 렌더 새 버전은 fb.py publish로만 추가) |
| `소스/` | 원본 클립·이미지·음악 | 읽기만, 수정·삭제 금지 |
| `타임라인/` | 리졸브에서 내보낸 FCPXML | 원본 읽기만, 새 버전 파일만 추가 |
| `작업/feedback/` | 영상별 피드백 `<이름>.feedback.json`, 화면 캡처(`snapshots/`) | JSON 직접 수정 금지 → fb.py |
| `작업/feedback/links.json` | 구글 드라이브 영상 링크·고객 피드백 연결 정보 | 읽기만 (비밀키 포함 — 출력하거나 옮기지 않기) |
| `작업/productions/` | 코드 렌더 영상의 제작 정보 (fb.py publish가 기록) | 읽기만 |
| `작업/feedback-tool/` | 검토 페이지, fb.py, fcpxml_edit.py | 요청이 없으면 수정하지 않기 |

## 명령
- `python3 작업/feedback-tool/fb.py` — `status` · `brief [영상]` · `list` · `done <id> "내용" --in <파일>` · `question <id> "질문"` · `skip <id> "이유"` · `reopen <id>` · `frame <영상> <TC>` · `publish` · `open` · `stop` · `sync [영상]`(고객 피드백 가져오기 — brief가 자동으로 함)
- `python3 작업/feedback-tool/fcpxml_edit.py` — `info` · `locate` · `apply` · `validate` · `next-name`

## 규칙
- 피드백 상태는 fb.py로만 바꾼다 (검토 페이지가 같은 파일을 동시에 쓴다).
- FCPXML 시간은 fcpxml_edit.py로 다룬다 (유리수·프레임 경계·리플 자동). 원본은 덮어쓰지 않는다.
- 애매한 피드백은 추측해서 고치지 말고 question, 할 수 없는 건 skip으로 남긴다.
- 사용자에게는 한국어로 짧게 보고한다: 반영·질문·보류 개수, 새 파일, 질문 목록, 리졸브 확인 포인트.
