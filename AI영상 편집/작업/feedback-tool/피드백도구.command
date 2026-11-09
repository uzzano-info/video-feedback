#!/bin/bash
# 더블클릭하면 피드백 검토 페이지가 켜지고 브라우저가 열려요. (끄기: python3 작업/feedback-tool/fb.py stop)
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3를 찾을 수 없어요. 터미널에서 'xcode-select --install' 로 설치한 뒤 다시 눌러 주세요."
  read -n 1 -s -r -p "아무 키나 누르면 닫혀요"; exit 1
fi
python3 fb.py open
echo ""
echo "검토 페이지가 열렸어요. 이 창은 닫아도 돼요 (페이지는 계속 켜져 있어요)."
sleep 3
