---
description: 검토 페이지에 남긴 피드백을 읽고 반영한다 (리졸브 FCPXML 새 버전 또는 코드 렌더 영상 새 버전)
argument-hint: "[영상 이름 일부 — 생략하면 가장 최근 피드백]"
allowed-tools: Bash(python3 작업/feedback-tool/fb.py:*), Bash(python3 작업/feedback-tool/fcpxml_edit.py:*), Bash(xmllint:*), Bash(ffprobe:*), Read, Grep, Glob, Write
---

# 피드백 반영 (/apply-feedback)

대상: $ARGUMENTS

## 지금 대기 현황
!`python3 작업/feedback-tool/fb.py status`

## 도구
- `python3 작업/feedback-tool/fb.py` — 피드백 읽기·상태 기록 (JSON은 직접 고치지 않는다)
- `python3 작업/feedback-tool/fcpxml_edit.py` — FCPXML 구조 보기·수정·검증 (시간 계산은 이 도구가 유리수로 한다)
- 경로에 공백이 있으니(`AI영상 편집`) 셸 명령의 경로는 항상 따옴표로 감싼다.

## 절차
1. **대상 영상 확인** — 말하지 않았으면 가장 최근에 수정된 feedback JSON 기준.
   `python3 작업/feedback-tool/fb.py brief [영상 이름 일부]`
   → 지시서에 영상·FCPXML·fps·타임라인 시작 TC, 그리고 **status가 todo인 항목만** 나온다.
2. **todo 항목 읽기** — 항목마다 영상 기준 시간(`start_tc`·`end_tc`), 프레임 번호, 유형, 메모가 나온다. 그 순간의 클립·자막과 화면 캡처 경로도 함께 나온다.
   "여기·이거·표시한 곳"처럼 화면을 가리키는 메모는 **스냅샷을 Read로 열어** 판단한다 (빨간 화살표·네모·펜 = 사용자가 가리킨 곳).
3. **제작 방식에 따라 갈라진다** (지시서의 "제작 방식" 줄)
   - 다빈치 리졸브 타임라인 → 4~6
   - Claude Code 코드 렌더 → 맨 아래 "코드 렌더 영상"
4. **컨펌본 타임코드 → 타임라인 위치 → 클립**
   - 컨펌본 0프레임 = 타임라인 시작(tcStart). 지시서의 "타임라인 위치"가 그 변환값이다.
   - `python3 작업/feedback-tool/fcpxml_edit.py info "타임라인/<파일>.fcpxml"` 으로 주 스토리라인과 연결 클립을 확인한다.
   - `... locate "타임라인/<파일>.fcpxml" <프레임>f` 으로 그 시점의 클립·자막을 확인한다.
   - 영상 길이와 FCPXML 길이가 다르면 렌더 범위가 다른 것이다. 위치가 어긋나므로 고치지 말고 모두 question으로 남기고 확인을 요청한다.
5. **FCPXML 수정** — 계획 파일(JSON)을 만들어 엔진으로 적용한다. 원본은 절대 덮어쓰지 않고, 다음 버전으로 자동 저장된다 (`intro_v2.fcpxml` → `intro_v3.fcpxml`).
   `python3 작업/feedback-tool/fcpxml_edit.py apply "타임라인/<파일>.fcpxml" /tmp/plan.json`
   시점은 **프레임 번호(`375f`)** 로 쓴다. 지시서의 `start_frame`·`end_frame`을 그대로 쓰면 정확하다.
   모든 시점은 **원래 영상 기준**으로 쓴다. 엔진이 뒤쪽부터 적용해서 서로 밀리지 않게 처리한다.

   | 유형 | 계획(op) | 기준 |
   |---|---|---|
   | 컷 삭제 | `{"id","op":"cut","start":"<start_frame>f","end":"<end_frame>f"}` | 구간(I/O)이 있을 때. 구간이 없으면 메모에 범위가 분명할 때만, 아니면 question |
   | 길이 조절 | `{"id","op":"length","at":"<프레임>f","delta_frames":±N,"edge":"end"\|"start"}` | "2초 줄여" → `-2×fps`프레임(반올림). 늘리기는 원본 소스 여유 안에서만 (엔진이 확인) |
   | 순서 변경 | `{"id","op":"move","clip":"<옮길 클립 프레임>f","to":"<기준 클립 프레임>f","position":"before"\|"after"}` | 어디로 옮길지 분명할 때만 |
   | 자막·텍스트 수정 | `{"id","op":"text","at":"<프레임>f","text":"새 문구","old":"지금 문구 일부"}` | 새 문구가 메모에 분명할 때만 |
   | 클립 교체 | `{"id","op":"replace","at":"<프레임>f","file":"소스/<파일>","lane":"top"}` | 파일이 `소스/`에 있을 때만 (지시서의 ✓/✗) |
   | 기타 | 위 작업으로 표현될 때만 | 아니면 question 또는 skipped |

   - 컷 삭제·길이 조절 뒤에는 엔진이 이후 클립의 offset을 모두 다시 계산한다 (빈 구멍·겹침 없음). 연결 클립(B롤·자막·배경음악)도 함께 정리한다.
   - 시간은 반드시 유리수(분수), 프레임 경계. **XML 시간을 손으로 계산하거나 부동소수점을 쓰지 않는다.**
   - 엔진이 거절한 항목(이유가 나옴)은 그 이유 그대로 question 또는 skipped로 남긴다.
6. **결과 검증** — apply 출력의 `validation`을 확인한다. 같은 검사를 따로 돌릴 수도 있다:
   `python3 작업/feedback-tool/fcpxml_edit.py validate "타임라인/<새 파일>.fcpxml" --base "타임라인/<원본>.fcpxml" --expect-delta-frames <예상 변화>`
   - 검사 항목: XML 문법(xmllint), 참조한 asset·효과가 모두 있는지, 클립 겹침·빈 구멍, 프레임 경계, 총 길이 변화가 삭제·조절한 길이와 맞는지
   - 하나라도 ✗이면 그 파일을 결과로 쓰지 않는다. 원인을 고치거나 해당 항목을 question으로 바꾼다.
7. **JSON 업데이트** (fb.py로만)
   - 반영: `python3 작업/feedback-tool/fb.py done <id> "무엇을 어떻게 바꿨는지 한 줄" --in <새 FCPXML 파일명>`
   - 의미가 애매함: `python3 작업/feedback-tool/fb.py question <id> "사용자에게 물어볼 내용"`
   - FCPXML만으로 할 수 없음(색보정, 효과, 소스에 없는 클립 요청 등): `python3 작업/feedback-tool/fb.py skip <id> "이유"`
   - 추측해서 고치지 않는다.
8. **변경 요약 출력** (한국어, 짧게)
   - 반영 n · 질문 n · 보류 n, 새 파일 경로
   - 질문 목록
   - 리졸브에서 확인할 포인트: 고친 지점의 타임코드, 잘린 연결 클립(B롤·자막·음악), `작업/README.md`의 체크리스트

## 코드 렌더 영상 (Claude Code가 만든 영상)
지시서에 프로젝트 폴더와 렌더 명령이 나온다.
1. 프로젝트에서 해당 장면의 코드·설정·자막·에셋을 찾아 메모대로 고친다. 시간, 자막 문구, 스냅샷을 단서로 쓴다.
2. 기록된 렌더 명령으로 다시 렌더한다. 실패하면 고쳐서 다시 시도하고, 끝내 안 되면 상태를 바꾸지 않고 보고한다.
3. `python3 작업/feedback-tool/fb.py publish <렌더 결과> --fixes <id,id> --note "바뀐 점"` → 새 버전이 `컨펌본/`에 올라간다.
4. 항목 기록: `fb.py done <id> "바꾼 내용" --in <새 영상 파일명>` (애매하면 question, 불가하면 skip)
5. 확인: `python3 작업/feedback-tool/fb.py frame <새 영상> <타임코드>` → Read로 열어 원래 스냅샷과 비교한다.

## 지킬 것
- `소스/`와 `컨펌본/` 파일은 읽기만 하고, 절대 수정·삭제하지 않는다. 원본 FCPXML도 덮어쓰지 않는다.
- todo 항목만 처리한다. done·question·skipped(특히 사용자가 직접 완료한 것)는 건드리지 않는다.
- 메모에 없는 "개선"은 하지 않는다. 필요해 보이면 요약에서 제안만 한다.
- `작성: 고객 ○○` 이 붙은 항목은 고객사가 고객 페이지에서 남긴 피드백이다. 애매하거나 편집 의도와 부딪히면 추측하지 말고 question으로 남긴다 (편집자가 고객과 확인). 상태를 바꾸면 고객 페이지에도 자동으로 알려진다.
- 구글 드라이브로 불러온 영상(`이름.gdrive`)은 맥에 영상 파일이 없다. FCPXML과 프레임 번호로 작업하고, 화면은 스냅샷으로 확인한다 (`fb.py frame` 은 쓸 수 없음).
- 검토 페이지의 "Claude Code에게 맡기기"로 시작된 실행(화면 없음)에서는 중간에 질문할 수 없다. 애매한 건 question으로 남긴다.
