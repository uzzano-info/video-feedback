# 만들기: (1) Code.gs = server.gs + 고객 페이지 HTML  (2) GitHub Pages용 고객 페이지 index.html
# 사용: python3 build.py <Code.gs 경로> [<pages 폴더>]
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, "server.gs"), encoding="utf-8").read()
html = open(os.path.join(here, "client.html"), encoding="utf-8").read()
out = src.rstrip() + "\n\n// ---------- 고객 페이지 화면 (자동 생성 — 고치지 마세요) ----------\nvar CLIENT_HTML = " + json.dumps(html, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029") + ";\n"
os.makedirs(os.path.dirname(os.path.abspath(sys.argv[1])), exist_ok=True)
open(sys.argv[1], "w", encoding="utf-8").write(out)
print(sys.argv[1], len(out))
if len(sys.argv) > 2:  # GitHub Pages: 영상 정보는 주소(?p=…&s=…)로 받아요
    pages = sys.argv[2]
    os.makedirs(pages, exist_ok=True)
    page = (html.replace("var BOOT = __BOOT__;", "var BOOT = null;")
                .replace('<meta charset="utf-8">', '<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>영상 피드백</title>\n<meta name="robots" content="noindex">', 1)
                .replace('<base target="_top">\n', '')
                .replace("<script>\nvar BOOT", '<script src="config.js"></script>\n<script>\nvar BOOT', 1))
    open(os.path.join(pages, "index.html"), "w", encoding="utf-8").write(page)
    cfg = os.path.join(pages, "config.js")
    if not os.path.exists(cfg):
        open(cfg, "w", encoding="utf-8").write("// 구글 Apps Script 배포 ID(AKfy…)를 넣으면 고객 링크에서 &s=… 를 생략할 수 있어요\nwindow.FB_BACKEND = '';\n")
    open(os.path.join(pages, ".nojekyll"), "w").close()
    open(os.path.join(pages, "Code.gs"), "w", encoding="utf-8").write(out)  # 편집자 홈의 "코드 복사"용
    print(pages, "index.html", len(page))
