#!/bin/bash
# 더블클릭하면 피드백 도구가 켜지고 브라우저가 열려요. 끄려면 이 창에서 Ctrl + C.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3를 찾을 수 없어요. 터미널에서 'xcode-select --install' 을 실행해 설치한 뒤 다시 눌러 주세요."
  read -n 1 -s -r -p "아무 키나 누르면 닫혀요"
  exit 1
fi
python3 server.py
