#!/usr/bin/env python3
"""피드백 도구 CLI — Claude Code가 피드백을 읽고, 상태를 바꾸고, 새 버전을 올릴 때 쓰는 명령.
(표준 라이브러리만 사용. 피드백 JSON은 직접 고치지 말고 이 명령을 쓰세요.)

사용:  python3 작업/feedback-tool/fb.py <명령> [...]

  status [--hook]                      영상별 대기·반영·보류 개수
  brief [영상]                          대기 피드백 작업 지시서 (영상 생략 시 대기가 있는 최신 영상)
  list [영상] [--all]                   피드백 한 줄씩 (기본: 대기만)
  done <id> "<바꾼 내용>" [--in 새파일]  반영 완료로 표시 (claude_note, resolved_in 기록)
  question <id> "<질문>"                애매해서 사용자에게 물어볼 항목으로 표시
  skip <id> "<보류 이유>"               할 수 없어서 보류로 표시
  reopen <id>                          다시 대기로
  frame <영상|경로> <시간> [--out 파일]   그 시점 화면을 jpg로 뽑기 (ffmpeg 필요)
  publish <렌더파일> [--base 이름] [--project 폴더] [--render "명령"] [--output 경로]
          [--note "바뀐 점"] [--fixes id,id]   컨펌본에 '이름_vN'으로 올리고 제작 정보 기록
  open [영상]                           검토 페이지 열기 (서버가 꺼져 있으면 켬)
  stop                                 검토 서버 끄기
  sync [영상]                           고객 피드백 가져오기 + 반영 상태 알리기 (고객 링크가 있는 영상)

영상은 파일명 전체나 일부로 지정해요 (예: '애플_v3', '애플').
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from urllib.parse import quote

sys.dont_write_bytecode = True
import server  # noqa: E402  (같은 폴더의 server.py 기능을 그대로 사용)
import remote  # noqa: E402
from fcpxml import nfc  # noqa: E402

D = server.Dirs(server.default_base())
PROD_DIR = os.path.join(D.work, "productions")
KO = {"todo": "대기", "done": "반영", "skipped": "보류", "question": "질문"}
TOOL_KO = {"pen": "펜", "arrow": "화살표", "rect": "네모"}


def die(msg):
    print("✗ " + msg, file=sys.stderr)
    sys.exit(1)


def rel(p):
    return os.path.relpath(p, D.base)


def videos():
    return server.list_video_files(D)


def fb_of(actual):
    try:
        return server.load_feedback(D, actual)
    except (OSError, ValueError):
        return {"items": []}


def counts(items):
    c = {"todo": 0, "done": 0, "skipped": 0, "question": 0}
    for i in items:
        st = i.get("status") or "todo"
        c[st] = c.get(st, 0) + 1
    return c


def fb_mtime(actual):
    p = server.feedback_path(D, actual)
    return os.path.getmtime(p) if os.path.exists(p) else 0


def resolve_video(arg):
    vids = videos()
    if not vids:
        die(f"컨펌본 폴더에 영상이 없어요 ({rel(D.confirm)})")
    if arg:
        a = nfc(arg).strip().lower()
        a_stem = os.path.splitext(a)[0]
        for v in vids:
            if v["name"].lower() == a or os.path.splitext(v["name"])[0].lower() == a_stem:
                return v["actual"]
        part = [v for v in vids if a_stem in v["name"].lower()]
        if len(part) == 1:
            return part[0]["actual"]
        if not part:
            die(f"'{arg}'에 맞는 영상이 없어요. 있는 영상: " + ", ".join(v["name"] for v in vids))
        withp = [v for v in part if counts(fb_of(v["actual"])["items"])["todo"]]
        if len(withp) == 1:
            return withp[0]["actual"]
        die(f"'{arg}'에 맞는 영상이 여러 개예요: " + ", ".join(v["name"] for v in part))
    pend = [v for v in vids if counts(fb_of(v["actual"])["items"])["todo"]]
    if pend:
        return max(pend, key=lambda v: fb_mtime(v["actual"]))["actual"]
    return max(vids, key=lambda v: v["mtime"])["actual"]


def parse_time(s, actual=None):
    """'12.5'(초) · '00:00:12:15'(HH:MM:SS:FF) · '01:12.5'(분:초) · '375f'(프레임) → 초"""
    s = str(s).strip()
    tm = server.timing(D, actual) if actual else None
    if s.endswith("f") and s[:-1].isdigit():
        return float(int(s[:-1]) * (tm["fd"] if tm else server.Fraction(1, 30)))
    if re.fullmatch(r"\d+:\d{2}:\d{2}[:;]\d{2}", s):
        h, m, sec, f = map(int, re.split(r"[:;]", s))
        base = tm["base"] if tm else 30
        fd = tm["fd"] if tm else server.Fraction(1, 30)
        return float(((h * 3600 + m * 60 + sec) * base + f) * fd)
    if re.fullmatch(r"[\d.]+", s):
        return float(s)
    t = 0.0
    for x in s.split(":"):
        t = t * 60 + float(x)
    return t


def production_desc(prod, tl_name):
    if prod:
        bits = ["Claude Code 코드 렌더"]
        if prod.get("project_dir"):
            bits.append(f"프로젝트 {prod['project_dir']}")
        if prod.get("render"):
            bits.append(f"렌더 명령 `{prod['render']}`")
        if prod.get("output"):
            bits.append(f"렌더 결과 {prod['output']}")
        return " · ".join(bits)
    if tl_name:
        return f"다빈치 리졸브 타임라인 (타임라인/{tl_name})"
    return "알 수 없음 (제작 정보·타임라인 모두 없음)"


# ---------- 명령 ----------
def cmd_status(args):
    vids = sorted(videos(), key=lambda v: v["name"].lower())
    lines = []
    total = 0
    for v in vids:
        c = counts(fb_of(v["actual"])["items"])
        total += c["todo"]
        tl = server.fcpxml.find_timeline(D.timeline, v["actual"])
        prod = server.production_for(D, v["actual"])
        kind = "코드 렌더" if prod else ("리졸브 타임라인" if tl else "제작 정보 없음")
        lines.append(f"- {v['name']} · 대기 {c['todo']} · 반영 {c['done']} · 질문 {c['question']} · 보류 {c['skipped']} · {kind}")
    if args.hook:  # Claude Code SessionStart 훅: 대기 피드백이 있을 때만 알려줌
        if total:
            text = ("[영상 피드백 도구] 대기 중인 피드백이 있어요.\n" + "\n".join(l for l in lines if "대기 0 " not in l)
                    + "\n사용자가 '피드백 반영해줘'라고 하면 /apply-feedback 절차를 따르세요.")
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}, ensure_ascii=False))
        return
    if not vids:
        print(f"컨펌본 폴더에 영상이 없어요 ({rel(D.confirm)})")
        return
    print(f"피드백 현황 · 대기 합계 {total}")
    print("\n".join(lines))


def span(it):
    t = it.get("start_tc", "")
    if it.get("end_tc"):
        t += "~" + it["end_tc"]
    return t


def item_line(it):
    extra = []
    if it.get("source_clip"):
        extra.append("화면=" + it["source_clip"])
    if it.get("drawing"):
        extra.append(f"그림 {len(it['drawing'])}")
    if it.get("claude_note"):
        extra.append("메모=" + it["claude_note"])
    if it.get("resolved_in"):
        extra.append("반영=" + it["resolved_in"])
    memo = " ".join(it.get("memo", "").split())
    return f"[{it['id']}] {span(it)} · {it.get('type', '기타')} · {KO.get(it.get('status', 'todo'), it.get('status'))} · {memo}" + (
        "  (" + " | ".join(extra) + ")" if extra else "")


def cmd_list(args):
    actual = resolve_video(args.video)
    items = fb_of(actual)["items"]
    shown = items if args.all else [i for i in items if i.get("status") == "todo"]
    print(f"{nfc(actual)} · {'전체' if args.all else '대기'} {len(shown)}개")
    for it in shown:
        print(item_line(it))


def try_sync(actual, quiet=False):
    """고객 링크가 있는 영상이면 고객 피드백을 먼저 가져옴 (인터넷이 안 돼도 작업은 계속)"""
    if not remote.is_link(actual) or not (remote.link_info(D, actual) or {}).get("token"):
        return None
    try:
        r = remote.sync(D, actual, server)
        if not quiet and (r.get("added") or r.get("changed") or r.get("removed")):
            print(f"(고객 피드백 동기화: 새 {r['added']} · 수정 {r['changed']} · 삭제 {r['removed']})")
        return r
    except Exception as e:
        if not quiet:
            print(f"(고객 피드백을 가져오지 못했어요 — {e}. 이미 받은 피드백으로 진행)")
        return None


CURRENT = os.path.join(D.feedback, ".current")  # 마지막으로 지시서를 본 영상 (done/question/skip 이 이 영상의 id를 먼저 찾음)


def cmd_brief(args):
    actual = resolve_video(args.video)
    try_sync(actual)
    try:
        with open(CURRENT, "w", encoding="utf-8") as f:
            f.write(nfc(actual))
    except OSError:
        pass
    fb = fb_of(actual)
    items = fb["items"]
    c = counts(items)
    tm = server.timing(D, actual)
    tl, tl_name, tl_err = server.get_timeline(D, actual)
    prod = server.production_for(D, actual)
    sources = {s.lower() for s in server.list_sources(D)}
    todo = [i for i in items if i.get("status") == "todo"]
    print(f"# 피드백 작업 지시서 — {nfc(actual)}")
    where = server.header_for(D, actual, tm)["video"] if remote.is_link(actual) else rel(os.path.join(D.confirm, actual))
    print(f"- 영상: {where} · {float(1 / tm['fd']):.3f}fps ({tm['fps']}, 기준: {tm['source']})")
    print(f"- 제작 방식: {production_desc(prod, tl_name)}")
    if tl_name:
        print(f"- FCPXML: 타임라인/{tl_name} · 타임라인 시작 {server.tc_of(tm['tc0_frames'], tm['base'])}"
              + (f" · 길이 {server.tc_of(int(round(server.Fraction(str(tl['duration'])) / tm['fd'])), tm['base'])}" if tl else ""))
        if tl_name and server.stem_of(tl_name) != server.stem_of(actual):
            print(f"  ⚠ 영상과 FCPXML 이름이 달라요 — 버전이 맞는지 먼저 확인할 것")
    if tl_err:
        print(f"- 타임라인 읽기 오류: {tl_err}")
    print(f"- 피드백 파일: {rel(server.feedback_path(D, actual))}")
    print(f"- 대기 {c['todo']} · 반영 {c['done']} · 질문 {c['question']} · 보류 {c['skipped']}")
    if prod and prod.get("versions"):
        last = prod["versions"][-1]
        print(f"- 최근 버전: v{last.get('version')} ({last.get('file')})" + (f" — {last['note']}" if last.get("note") else ""))
    if not todo:
        others = [v["name"] for v in videos() if v["actual"] != actual and counts(fb_of(v["actual"])["items"])["todo"]]
        print("\n대기 중인 피드백이 없어요." + (" 대기가 있는 다른 영상: " + ", ".join(others) if others else ""))
        return
    print("\n상태 기록: `fb.py done <id> \"바꾼 내용\" --in <새 FCPXML>` · `fb.py question <id> \"질문\"` · `fb.py skip <id> \"이유\"`")
    print("시점은 영상 기준(영상 시작 = 00:00:00:00). fcpxml_edit.py 에는 프레임 번호(예: 375f)를 그대로 쓰면 정확해요.")
    for n, it in enumerate(todo, 1):
        sf, ef = it.get("start_frame"), it.get("end_frame")
        frames = f"{sf}f" + (f"~{ef}f, {ef - sf}프레임" if ef is not None else "")
        print(f"\n## {n}. [{it['id']}] {span(it)} ({frames}) · {it.get('type', '기타')}")
        print(f"메모: {it.get('memo', '')}")
        if it.get("origin") == "client":
            print(f"작성: 고객 {it.get('author') or ''} (고객 페이지에서 남긴 피드백 — 애매하면 question으로 남기면 편집자가 고객에게 확인해요)")
        print(f"타임라인 위치: {it.get('timeline_start_tc')}" + (f" ~ {it['timeline_end_tc']}" if it.get("timeline_end_tc") else ""))
        scr = []
        if it.get("source_clip"):
            s = f"클립 {it['source_clip']}"
            if it.get("lane") is not None:
                s += f" (lane {it['lane']}" + (", 연결 클립(B롤)" if it.get("lane", 0) > 0 else "") + ")"
            scr.append(s)
        if it.get("subtitle"):
            scr.append(f"자막 \"{it['subtitle']}\"")
        if scr:
            print("화면: " + " · ".join(scr))
        if it.get("mentioned_files"):
            print("언급 파일: " + ", ".join(f"{m} {'✓소스에 있음' if m.lower() in sources else '✗소스에 없음'}" for m in it["mentioned_files"]))
        if it.get("drawing"):
            kinds = {}
            for sh in it["drawing"]:
                k = TOOL_KO.get(sh["tool"], sh["tool"])
                kinds[k] = kinds.get(k, 0) + 1
            print("그림: " + ", ".join(f"{k} {v}" for k, v in kinds.items()) + " (사용자가 화면에 직접 표시)")
        if it.get("snapshot"):
            print(f"스냅샷: {os.path.join(D.feedback, it['snapshot'])}  ← Read로 열어서 확인")


def set_status(item_id, status, text, video=None, resolved_in=None):
    targets = [resolve_video(video)] if video else [v["actual"] for v in videos()]
    extra = []  # 컨펌본에서 빠진 영상의 JSON도 찾을 수 있게
    if not video and os.path.isdir(D.feedback):
        known = {os.path.basename(server.feedback_path(D, a)) for a in targets}
        for f in os.listdir(D.feedback):
            if f.endswith(".feedback.json") and not f.startswith(".") and f not in known:
                extra.append(f[: -len(".feedback.json")] + ".mp4")
    if not video:  # 같은 id(f001 등)가 여러 영상에 있을 수 있어서: 마지막 지시서 영상 → 하나뿐일 때만 자동
        cands = [a for a in targets + extra if os.path.exists(server.feedback_path(D, a))
                 and any(i.get("id") == item_id for i in server.load_feedback(D, a)["items"])]
        try:
            with open(CURRENT, "r", encoding="utf-8") as f:
                cur = f.read().strip()
        except OSError:
            cur = ""
        pick = [a for a in cands if nfc(a) == cur] or (cands if len(cands) == 1 else [])
        if not pick and len(cands) > 1:
            die(f"id '{item_id}' 가 여러 영상에 있어요: {', '.join(nfc(a) for a in cands)} — -v <영상> 으로 지정해 주세요")
        targets, extra = pick, []
    with server.file_lock(D):
        for actual in targets + extra:
            if not os.path.exists(server.feedback_path(D, actual)):
                continue
            data = server.load_feedback(D, actual)
            for it in data["items"]:
                if it.get("id") == item_id:
                    it["status"] = status
                    it["claude_note"] = text or ""
                    if status == "done":
                        it["resolved_by"] = "claude"
                        it["resolved_in"] = resolved_in
                    else:
                        it.pop("resolved_by", None)
                        it["resolved_in"] = None
                    it["updated_at"] = server.now_iso()
                    server.save_feedback(D, actual, data)
                    return nfc(actual), it
    die(f"id '{item_id}' 피드백을 찾을 수 없어요 (fb.py list --all 로 확인)")


def notify_client(video, it):
    """고객 피드백이면 고객 페이지에도 상태를 알림 (실패해도 다음 동기화 때 다시 알려요)"""
    if it.get("remote_id"):
        actual = server.resolve_video(D, video)
        if actual:
            try_sync(actual, quiet=True)


def cmd_sync(args):
    targets = [resolve_video(args.video)] if args.video else [v["actual"] for v in videos() if remote.is_link(v["actual"])]
    any_ = False
    for actual in targets:
        info = remote.link_info(D, actual) or {}
        if not info.get("token"):
            continue
        any_ = True
        try:
            r = remote.sync(D, actual, server)
            print(f"{nfc(actual)}: 새 {r['added']} · 수정 {r['changed']} · 삭제 {r['removed']}  (고객 링크 {r.get('client_url')})")
        except remote.RemoteError as e:
            print(f"{nfc(actual)}: 가져오지 못했어요 — {e}")
    if not any_:
        print("고객 링크가 있는 영상이 없어요. 검토 페이지의 🌐 고객 링크에서 만들어 주세요.")


def cmd_done(args):
    if not args.text.strip():
        die("무엇을 어떻게 바꿨는지 한 줄로 적어 주세요")
    rin = os.path.basename(args.resolved_in) if args.resolved_in else None
    v, it = set_status(args.id, "done", args.text.strip(), args.video, rin)
    print(f"✓ [{it['id']}] {span(it)} 반영 — {it['claude_note']}" + (f" → {rin}" if rin else "") + f"  ({v})")
    notify_client(v, it)


def cmd_question(args):
    if not args.text.strip():
        die("사용자에게 물어볼 내용을 적어 주세요")
    v, it = set_status(args.id, "question", args.text.strip(), args.video)
    print(f"? [{it['id']}] {span(it)} 질문 — {it['claude_note']}  ({v})")
    notify_client(v, it)


def cmd_skip(args):
    if not args.text.strip():
        die("보류 이유를 적어 주세요")
    v, it = set_status(args.id, "skipped", args.text.strip(), args.video)
    print(f"⏸ [{it['id']}] {span(it)} 보류 — {it['claude_note']}  ({v})")
    notify_client(v, it)


def cmd_reopen(args):
    v, it = set_status(args.id, "todo", "", args.video)
    print(f"↺ [{it['id']}] {span(it)} 다시 대기  ({v})")
    notify_client(v, it)


def cmd_frame(args):
    ff = shutil.which("ffmpeg")
    if not ff:
        die("ffmpeg가 없어요. 'brew install ffmpeg'로 설치하면 쓸 수 있어요")
    actual = None if os.path.isfile(args.video) else resolve_video(args.video)
    path = args.video if actual is None else os.path.join(D.confirm, actual)
    t = parse_time(args.time, actual)
    out = args.out
    if not out:
        folder = os.path.join(D.feedback, "frames")
        os.makedirs(folder, exist_ok=True)
        out = os.path.join(folder, f"{os.path.splitext(nfc(os.path.basename(path)))[0]}_{t:.1f}s.jpg")
    r = subprocess.run([ff, "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", "-q:v", "3", out],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        die("화면을 뽑지 못했어요: " + (r.stderr.strip() or "알 수 없는 오류"))
    print(os.path.abspath(out))


def next_version(base, ext):
    pat = re.compile(re.escape(base.lower()) + r"(?:_v(\d+))?$")
    n = 0
    for v in videos():
        m = pat.fullmatch(os.path.splitext(v["name"])[0].lower())
        if m:
            n = max(n, int(m.group(1) or 1))
    return n + 1


def cmd_publish(args):
    src = os.path.abspath(os.path.expanduser(args.file))
    if not os.path.isfile(src):
        die(f"렌더 파일이 없어요: {args.file}")
    ext = os.path.splitext(src)[1].lower()
    if ext not in server.VIDEO_EXTS:
        die(f"검토 페이지에서 재생할 수 없는 형식이에요 ({ext}). mp4(H.264)로 렌더해 주세요")
    base = args.base
    if not base and os.path.isdir(PROD_DIR):  # 이름을 안 줬으면: 같은 렌더 결과 경로를 쓰던 영상의 이름을 이어 씀
        for f in sorted(os.listdir(PROD_DIR)):
            try:
                with open(os.path.join(PROD_DIR, f), "r", encoding="utf-8") as fh:
                    m0 = json.load(fh)
            except (OSError, ValueError):
                continue
            if m0.get("output") and os.path.abspath(m0["output"]) == src:
                base = m0.get("base")
    base = nfc(base or server.version_base(os.path.splitext(os.path.basename(src))[0])).strip()
    if not base or "/" in base:
        die("--base 이름이 올바르지 않아요")
    os.makedirs(D.confirm, exist_ok=True)
    n = next_version(base, ext)
    dest_name = f"{base}_v{n}{ext}"
    shutil.copy2(src, os.path.join(D.confirm, dest_name))
    os.makedirs(PROD_DIR, exist_ok=True)
    mpath = os.path.join(PROD_DIR, base + ".json")
    m = {}
    if os.path.exists(mpath):
        with open(mpath, "r", encoding="utf-8") as f:
            m = json.load(f)
    m["base"] = base
    if args.project:
        m["project_dir"] = os.path.abspath(os.path.expanduser(args.project))
    if args.render:
        m["render"] = args.render
    m["output"] = os.path.abspath(os.path.expanduser(args.output)) if args.output else m.get("output", src)
    m.setdefault("versions", []).append({
        "version": n, "file": dest_name, "rendered_from": src, "created_at": server.now_iso(),
        "note": args.note or "", "fixes": [x for x in (args.fixes or "").split(",") if x.strip()],
    })
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"✓ 컨펌본/{dest_name} 로 올렸어요 (v{n})")
    print(f"  제작 정보: {rel(mpath)}")
    info = running_server()
    print(f"  검토 페이지: {info['url']}?video={quote(dest_name)}" if info else "  검토 페이지가 꺼져 있어요 → fb.py open " + base)


def running_server():
    p = os.path.join(D.feedback, ".server.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            info = json.load(f)
        urllib.request.urlopen(info["url"] + "api/state", timeout=1.5).read(1)
        return info
    except Exception:
        return None


def _server_version(info):
    try:
        return json.loads(urllib.request.urlopen(info["url"] + "api/state", timeout=2).read()).get("tool_version")
    except Exception:
        return None


def cmd_open(args):
    name = nfc(resolve_video(args.video)) if args.video else None
    info = running_server()
    if info and _server_version(info) != server.TOOL_VERSION:  # 도구가 업데이트됐으면 예전 서버를 새로 켬
        try:
            os.kill(int(info["pid"]), 15)
        except (OSError, ValueError, KeyError):
            pass
        for _ in range(25):
            time.sleep(0.2)
            if not running_server():
                break
        info = None
        print("검토 서버를 새 버전으로 다시 켜요")
    if not info:
        log = open(os.path.join(D.feedback, ".server.log"), "a") if os.path.isdir(D.feedback) else subprocess.DEVNULL
        subprocess.Popen([sys.executable, os.path.join(server.HERE, "server.py"), "--no-browser"],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(40):
            time.sleep(0.25)
            info = running_server()
            if info:
                break
        if not info:
            die("검토 서버를 켜지 못했어요. 터미널에서 start.command 를 실행해 주세요")
        print("검토 서버를 켰어요")
    url = info["url"] + (f"?video={quote(name)}" if name else "")
    if args.if_videos and not videos():
        print(f"검토 페이지 준비됨 (아직 컨펌본 영상이 없어서 브라우저는 열지 않았어요): {url}")
        return
    if not args.no_browser:
        webbrowser.open(url)
    print(url)


def cmd_stop(args):
    p = os.path.join(D.feedback, ".server.json")
    info = running_server()
    if not info:
        print("검토 서버가 꺼져 있어요")
        return
    try:
        os.kill(int(info["pid"]), 15)
    except (OSError, ValueError, KeyError):
        die("검토 서버를 끄지 못했어요. 서버를 켠 터미널 창에서 Ctrl + C 를 눌러 주세요")
    for _ in range(20):
        time.sleep(0.2)
        if not os.path.exists(p):
            break
    print("검토 서버를 껐어요")


def main():
    ap = argparse.ArgumentParser(prog="fb.py", description="영상 피드백 도구 CLI (Claude Code용)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("status"); p.add_argument("--hook", action="store_true"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("brief"); p.add_argument("video", nargs="?"); p.set_defaults(fn=cmd_brief)
    p = sub.add_parser("list"); p.add_argument("video", nargs="?"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_list)
    for name, fn in (("done", cmd_done), ("skip", cmd_skip), ("question", cmd_question)):
        p = sub.add_parser(name); p.add_argument("id"); p.add_argument("text"); p.add_argument("-v", "--video"); p.set_defaults(fn=fn)
        if name == "done":
            p.add_argument("--in", dest="resolved_in", help="반영한 새 FCPXML 또는 새 영상 파일 이름")
    p = sub.add_parser("reopen"); p.add_argument("id"); p.add_argument("-v", "--video"); p.set_defaults(fn=cmd_reopen)
    p = sub.add_parser("frame"); p.add_argument("video"); p.add_argument("time"); p.add_argument("--out"); p.set_defaults(fn=cmd_frame)
    p = sub.add_parser("publish")
    p.add_argument("file"); p.add_argument("--base"); p.add_argument("--project"); p.add_argument("--render")
    p.add_argument("--output"); p.add_argument("--note"); p.add_argument("--fixes")
    p.set_defaults(fn=cmd_publish)
    p = sub.add_parser("open"); p.add_argument("video", nargs="?"); p.add_argument("--no-browser", action="store_true")
    p.add_argument("--if-videos", action="store_true", help="컨펌본에 영상이 있을 때만 브라우저 열기"); p.set_defaults(fn=cmd_open)
    p = sub.add_parser("stop"); p.set_defaults(fn=cmd_stop)
    p = sub.add_parser("sync"); p.add_argument("video", nargs="?"); p.set_defaults(fn=cmd_sync)
    args = ap.parse_args()
    if not getattr(args, "fn", None):
        print(__doc__)
        return
    args.fn(args)


if __name__ == "__main__":
    main()
