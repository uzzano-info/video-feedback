#!/bin/bash
# 더블클릭 한 번으로: Claude Code 설정 확인 → 검토 페이지 켜기 → Claude Code 시작
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
TOOL="$ROOT/작업/feedback-tool"
pause() { echo; read -n 1 -s -r -p "아무 키나 누르면 닫혀요"; echo; }

clear
echo "🎬 AI영상 편집"
echo "────────────────────────────────────────"
if ! python3 -c "import sys" >/dev/null 2>&1; then
  echo "✗ python3가 없어요. 터미널에서 'xcode-select --install' 로 설치한 뒤 다시 열어 주세요."
  pause; exit 1
fi

echo "① Claude Code 설정"
if bash "$TOOL/claude_setup/install.sh" --quiet; then
  echo "   ✓ /fb-apply · /fb-publish 준비됨"
else
  echo "   ⚠ 설정을 설치하지 못했어요 (그래도 계속 진행해요)"
fi

echo "② 검토 페이지"
python3 "$TOOL/fb.py" open --if-videos 2>&1 | sed 's/^/   /'
python3 "$TOOL/fb.py" status 2>&1 | sed 's/^/   /'
command -v ffmpeg >/dev/null 2>&1 || echo "   (참고) ffmpeg가 없어서 반영 후 화면 확인은 못 해요 → brew install ffmpeg"

echo "③ Claude Code"
CLAUDE="$(command -v claude 2>/dev/null)"
for c in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
  [ -z "$CLAUDE" ] && [ -x "$c" ] && CLAUDE="$c"
done
[ -z "$CLAUDE" ] && CLAUDE="$(zsh -lic 'command -v claude' 2>/dev/null | tail -1)"
if [ -z "$CLAUDE" ] || [ ! -x "$CLAUDE" ]; then
  echo "   Claude Code가 아직 설치돼 있지 않아요."
  read -r -p "   지금 설치할까요? (y/n) " yn
  if [ "$yn" = "y" ] || [ "$yn" = "Y" ]; then
    curl -fsSL https://claude.ai/install.sh | bash   # Anthropic 공식 설치 명령
    CLAUDE="$HOME/.local/bin/claude"
  fi
  if [ ! -x "$CLAUDE" ]; then
    echo "✗ Claude Code를 실행할 수 없어요. 설치 안내: https://docs.claude.com/en/docs/claude-code/setup"
    pause; exit 1
  fi
  echo "   ✓ 설치했어요. 처음 실행하면 브라우저에서 로그인 화면이 열려요."
fi
echo "   만들 영상을 말하거나, 검토가 끝났으면 \"피드백 반영해줘\"라고 하세요."
echo "   검토 페이지 끄기: python3 작업/feedback-tool/fb.py stop"
echo "────────────────────────────────────────"
echo "   잠시 후 Claude Code가 열려요…"
sleep 2
exec "$CLAUDE"
