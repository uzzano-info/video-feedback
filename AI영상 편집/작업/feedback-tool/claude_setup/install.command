#!/bin/bash
# Claude Code 설정만 따로 설치할 때 더블클릭 (보통은 '시작하기'가 알아서 해요)
bash "$(dirname "$0")/install.sh"
read -n 1 -s -r -p "아무 키나 누르면 닫혀요"; echo
