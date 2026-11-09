#!/bin/bash
# Claude Code 설정(.claude/)을 'AI영상 편집' 폴더에 설치·업데이트해요. 여러 번 실행해도 안전해요.
#  - .claude/commands/apply-feedback.md  (/apply-feedback 명령)
#  - .claude/skills/fb-apply, fb-publish   ("피드백 반영해줘", 새 버전 올리기)
#  - .claude/settings.json                 (세션 시작 알림 훅, 도구 실행 허용)
# 사용: bash install.sh [--quiet]
QUIET=0; [ "$1" = "--quiet" ] && QUIET=1
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
say() { [ "$QUIET" = 1 ] || echo "$@"; }
say "설치 위치: $ROOT/.claude"
mkdir -p "$ROOT/.claude/skills" "$ROOT/.claude/commands" || exit 1
cp -R "$HERE/skills/." "$ROOT/.claude/skills/" || exit 1
cp -R "$HERE/commands/." "$ROOT/.claude/commands/" || exit 1
rm -f "$ROOT/.claude/skills/fb-apply/resolve-rules.md"   # 예전 버전 파일 (지금은 apply-feedback 절차에 합쳐짐)
python3 - "$ROOT/.claude/settings.json" "$HERE/settings.json" "$QUIET" <<'PY' || exit 1
import json, os, shutil, sys
dst, src, quiet = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
new = json.load(open(src, encoding="utf-8"))
if not os.path.exists(dst):
    shutil.copy(src, dst)
    quiet or print("settings.json 새로 만들었어요")
    sys.exit()
cur = json.load(open(dst, encoding="utf-8"))
before = json.dumps(cur, sort_keys=True)
old_path = "feedback_tool"  # 예전 폴더 이름을 쓰는 규칙·훅은 지움
allow = [r for r in cur.setdefault("permissions", {}).setdefault("allow", []) if old_path not in r]
for r in new["permissions"]["allow"]:
    if r not in allow:
        allow.append(r)
cur["permissions"]["allow"] = allow
ss = cur.setdefault("hooks", {}).setdefault("SessionStart", [])
for g in ss:
    g["hooks"] = [h for h in g.get("hooks", []) if old_path not in h.get("command", "")]
ss[:] = [g for g in ss if g.get("hooks")]
cmd = new["hooks"]["SessionStart"][0]["hooks"][0]["command"]
if not any(h.get("command") == cmd for g in ss for h in g.get("hooks", [])):
    ss.append(new["hooks"]["SessionStart"][0])
if json.dumps(cur, sort_keys=True) == before:
    quiet or print("settings.json은 이미 최신이에요")
    sys.exit()
shutil.copy(dst, dst + ".bak")  # 바꾸기 전 원래 파일 보관
json.dump(cur, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
quiet or print("기존 settings.json을 업데이트했어요 (원래 파일은 settings.json.bak)")
PY
say "✓ 설치 끝"
