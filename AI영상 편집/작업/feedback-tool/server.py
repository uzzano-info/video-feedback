#!/usr/bin/env python3
"""영상 편집 피드백 도구 — 내 맥 전용 로컬 서버 (Python 표준 라이브러리만 사용)

실행:  python3 server.py            (또는 start.command 더블클릭)
종료:  터미널에서 Ctrl + C

폴더 구조 (이 파일은 작업/feedback-tool/ 안에 있어야 해요)
  AI영상 편집/컨펌본      렌더된 영상
  AI영상 편집/타임라인    다빈치 리졸브에서 내보낸 FCPXML
  AI영상 편집/소스        원본 클립
  AI영상 편집/작업/feedback/*.json   남긴 피드백 (영상 1개당 JSON 1개)
"""
import argparse
import atexit
import base64
import fcntl
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

import claude_job
import fcpxml
import remote
from fcpxml import nfc

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_VERSION = "2026.11.09-web"  # 바뀌면 fb.py open 이 예전 서버를 새 서버로 바꿔 켬
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")
SOURCE_EXTS = (".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mkv", ".mp3", ".wav", ".aac", ".m4a",
               ".jpg", ".jpeg", ".png", ".heic", ".gif", ".psd")
LOCK = threading.RLock()
MAX_BODY = 12 * 1024 * 1024  # 스냅샷 이미지 포함
SNAP_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}
# 피드백 유형 (화면 버튼과 같은 순서) → 리졸브 마커 색
TYPES = ["컷 삭제", "길이 조절", "순서 변경", "자막·텍스트 수정", "클립 교체", "기타"]
RESOLVE_COLORS = {"컷 삭제": "ResolveColorRed", "길이 조절": "ResolveColorYellow", "순서 변경": "ResolveColorPurple",
                  "자막·텍스트 수정": "ResolveColorBlue", "클립 교체": "ResolveColorCyan", "기타": "ResolveColorPink"}
# 예전 버전에서 쓰던 이름 → 새 이름
LEGACY_TYPES = {"컷": "컷 삭제", "B롤 바꾸기": "클립 교체", "자막 고치기": "자막·텍스트 수정", "B롤 빼기": "기타", "크기·위치": "기타"}
STATUSES = ("todo", "done", "skipped", "question")  # 대기 / 반영됨 / 보류 / 질문


# ---------- 폴더 ----------
class Dirs:
    def __init__(self, base):
        self.base = base
        self.work = os.path.join(base, "작업")
        self.confirm = self._pick("컨펌본", "컴펌본")
        self.timeline = os.path.join(base, "타임라인")
        self.source = os.path.join(base, "소스")
        self.feedback = os.path.join(self.work, "feedback")

    def _pick(self, *names):
        for n in names:
            p = os.path.join(self.base, n)
            if os.path.isdir(p):
                return p
        return os.path.join(self.base, names[0])


def default_base():
    # 이 파일 위치: <기준폴더>/작업/feedback-tool/server.py  (자체 테스트 때는 FEEDBACK_BASE 로 바꿀 수 있음)
    return os.environ.get("FEEDBACK_BASE") or os.path.dirname(os.path.dirname(HERE))


# ---------- 영상·소스 목록 ----------
def list_video_files(d):
    out = []
    try:
        names = os.listdir(d.confirm)
    except OSError:
        return out
    for n in names:
        if n.startswith("."):
            continue
        p = os.path.join(d.confirm, n)
        if n.lower().endswith(VIDEO_EXTS) and os.path.isfile(p):
            st = os.stat(p)
            out.append({"actual": n, "name": nfc(n), "size": st.st_size, "mtime": st.st_mtime})
    return out + remote.list_links(d)  # 구글 드라이브 링크로 등록한 영상 (이름.gdrive)


def resolve_video(d, name):
    name = nfc(name or "")
    for v in list_video_files(d):
        if v["name"] == name:
            return v["actual"]
    return None


_src_cache = {"t": 0, "dir": None, "names": []}


def list_sources(d):
    now = time.time()
    if _src_cache["dir"] == d.source and now - _src_cache["t"] < 10:
        return _src_cache["names"]
    names = set()
    root_depth = d.source.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(d.source):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        if root.count(os.sep) - root_depth >= 4:
            dirs[:] = []
        for f in files:
            if not f.startswith(".") and f.lower().endswith(SOURCE_EXTS):
                names.add(nfc(f))
        if len(names) > 8000:
            break
    out = sorted(names, key=str.lower)
    _src_cache.update(t=now, dir=d.source, names=out)
    return out


# ---------- 타임라인 ----------
_tl_cache = {}


def get_timeline(d, video_actual):
    """(파싱결과 또는 None, 타임라인 파일명 또는 None, 오류 메시지 또는 None)"""
    fname = fcpxml.find_timeline(d.timeline, video_actual)
    if not fname:
        return None, None, None
    path = os.path.join(d.timeline, fname)
    try:
        mtime = os.path.getmtime(path)
        key = (path, mtime)
        if key not in _tl_cache:
            _tl_cache.clear()
            _tl_cache[key] = fcpxml.parse_timeline(path)
        return _tl_cache[key], nfc(fname), None
    except Exception as e:  # 형식이 깨졌거나 지원하지 않는 경우
        return None, nfc(fname), f"{type(e).__name__}: {e}"


def segment_at(tl, t):
    segs = tl["segments"] if tl else []
    for s in segs:
        if s["start"] <= t < s["end"]:
            return s
    if segs and abs(t - segs[-1]["end"]) < 0.001:
        return segs[-1]
    return None


def subtitle_at(tl, t):
    if not tl:
        return ""
    hit = [s for s in tl["subtitles"] if s["start"] <= t < s["end"]]
    hit.sort(key=lambda s: -s["lane"])
    return " / ".join(s["text"] for s in hit)


# ---------- 피드백 JSON ----------
@contextmanager
def file_lock(d):
    """서버(화면)와 fb.py(Claude Code)가 같은 JSON을 동시에 고쳐도 내용이 사라지지 않게 잠근다."""
    os.makedirs(d.feedback, exist_ok=True)
    with LOCK:
        with open(os.path.join(d.feedback, ".lock"), "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def version_base(stem):
    """'영상_v3' → '영상'"""
    return re.sub(r"[\s_\-]*v\d+$", "", nfc(stem), flags=re.I)


def production_for(d, video_actual):
    """Claude Code가 코드로 렌더한 영상이면 제작 정보(작업/productions/<이름>.json)를 돌려준다."""
    folder = os.path.join(d.work, "productions")
    if not os.path.isdir(folder):
        return None
    name = nfc(video_actual)
    base = version_base(os.path.splitext(name)[0]).lower()
    fallback = None
    for f in sorted(os.listdir(folder)):
        if not f.endswith(".json") or f.startswith("."):
            continue
        try:
            with open(os.path.join(folder, f), "r", encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        if name in {nfc(v.get("file", "")) for v in m.get("versions", [])}:
            return m
        if nfc(m.get("base", "")).lower() == base:
            fallback = m
    return fallback


def stem_of(video_actual):
    return os.path.splitext(nfc(video_actual))[0]


def feedback_path(d, video_actual):
    """작업/feedback/<영상이름>.feedback.json"""
    return os.path.join(d.feedback, stem_of(video_actual) + ".feedback.json")


_fps_cache = {}


def probe_fps(path):
    """FCPXML이 없는 영상의 프레임레이트 (ffprobe가 있을 때만, 없으면 None)"""
    ff = shutil.which("ffprobe")
    if not ff:
        return None
    key = (path, os.path.getmtime(path))
    if key not in _fps_cache:
        try:
            r = subprocess.run([ff, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                                "-of", "csv=p=0", path], capture_output=True, text=True, timeout=10)
            _fps_cache[key] = Fraction(r.stdout.strip().split("\n")[0]) if r.stdout.strip() else None
        except Exception:
            _fps_cache[key] = None
    return _fps_cache[key]


def timing(d, video_actual):
    """이 영상의 프레임 기준: {fd, tc0, base, fps, tc_start, source, timeline}"""
    tl, name, _err = get_timeline(d, video_actual)
    if tl:
        fd, tc0, src = Fraction(tl["frame_duration_frac"]), Fraction(tl["tc_start_frac"]), "fcpxml"
    elif remote.is_link(video_actual):  # 구글 드라이브 영상: 등록할 때 고른 프레임레이트
        info = remote.link_info(d, video_actual) or {}
        fps = Fraction(info["fps"]) if info.get("fps") else None
        fd, tc0, src = (1 / fps, Fraction(0), "link") if fps else (Fraction(1, 30), Fraction(0), "default")
    else:
        fps = probe_fps(os.path.join(d.confirm, video_actual))
        fd, tc0, src = (1 / fps, Fraction(0), "video") if fps else (Fraction(1, 30), Fraction(0), "default")
    base = int(round(float(1 / fd)))
    fps = 1 / fd
    return {"fd": fd, "tc0": tc0, "base": base, "fps": f"{fps.numerator}/{fps.denominator}",
            "tc0_frames": int(round(tc0 / fd)), "source": src, "timeline": name, "tl": tl}


def tc_of(frames, base):
    """프레임 번호 → HH:MM:SS:FF"""
    n = int(frames)
    f, s = n % base, n // base
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def header_for(d, video_actual, tm=None):
    tm = tm or timing(d, video_actual)
    info = remote.link_info(d, video_actual) if remote.is_link(video_actual) else None
    return {"video": ("구글드라이브: " + remote.view_url(info["drive_id"])) if info else "컨펌본/" + nfc(video_actual),
            "fcpxml": ("타임라인/" + tm["timeline"]) if tm["timeline"] else None,
            "fps": tm["fps"], "fps_source": tm["source"],
            "timeline_tc_start": tc_of(tm["tc0_frames"], tm["base"])}


def _place(item, start_f, end_f, tm):
    """프레임 번호로 시간 관련 필드를 모두 채움 (영상 기준 + 타임라인 기준)"""
    fd, base, t0 = tm["fd"], tm["base"], tm["tc0_frames"]
    item["start_tc"] = tc_of(start_f, base)
    item["end_tc"] = tc_of(end_f, base) if end_f is not None else None
    item["start_seconds"] = round(float(start_f * fd), 3)
    item["end_seconds"] = round(float(end_f * fd), 3) if end_f is not None else None
    item["start_frame"] = int(start_f)
    item["end_frame"] = int(end_f) if end_f is not None else None
    item["timeline_start_tc"] = tc_of(start_f + t0, base)
    item["timeline_end_tc"] = tc_of(end_f + t0, base) if end_f is not None else None


ITEM_ORDER = ["id", "start_tc", "end_tc", "start_seconds", "end_seconds", "start_frame", "end_frame",
              "timeline_start_tc", "timeline_end_tc", "type", "memo", "status", "claude_note", "resolved_in"]


def ordered(item):
    out = {k: item[k] for k in ITEM_ORDER if k in item}
    out.update({k: v for k, v in item.items() if k not in out})
    return out


def migrate(old, tm):
    """예전 형식(<영상>.json: note/time/pending)을 새 형식으로"""
    items = []
    for n, it in enumerate(sorted(old.get("items", []), key=lambda x: x.get("time", 0)), 1):
        sf = int(round(Fraction(str(it.get("time", 0))) / tm["fd"]))
        ef = int(round(Fraction(str(it["time_end"])) / tm["fd"])) if it.get("time_end") else None
        new = {"id": f"f{n:03d}"}
        _place(new, sf, ef, tm)
        st = it.get("status", "pending")
        new.update({"type": LEGACY_TYPES.get(it.get("type"), it.get("type") or "기타"), "memo": it.get("note", ""),
                    "status": "todo" if st == "pending" else st, "claude_note": it.get("result", ""), "resolved_in": None})
        for k in ("source_clip", "subtitle", "mentioned_files", "drawing", "snapshot", "lane", "clip_range",
                  "source_time", "created_at", "resolved_by"):
            if k in it:
                new[k] = it[k]
        items.append(new)
    return items


def load_feedback(d, video_actual):
    p = feedback_path(d, video_actual)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("items", [])
        for it in data["items"]:  # 혹시 남은 예전 상태 이름 정리
            if it.get("status") in (None, "pending"):
                it["status"] = "todo"
        return data
    data = header_for(d, video_actual)
    data["items"] = []
    legacy = os.path.join(d.feedback, stem_of(video_actual) + ".json")
    if os.path.exists(legacy):  # 예전 파일이 있으면 자동으로 옮겨 옴 (원본은 .migrated로 남김)
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                data["items"] = migrate(json.load(f), timing(d, video_actual))
            save_feedback(d, video_actual, data)
            os.replace(legacy, legacy + ".migrated")
        except (OSError, ValueError):
            pass
    return data


def save_feedback(d, video_actual, data):
    os.makedirs(d.feedback, exist_ok=True)
    data["items"] = [ordered(it) for it in data.get("items", [])]
    p = feedback_path(d, video_actual)
    fd, tmp = tempfile.mkstemp(dir=d.feedback, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, p)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def timecode(t):
    t = max(0.0, float(t))
    tenth = int(t * 10 + 1e-6) % 10
    whole = int(t + 1e-6)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    return (f"{h}:{m:02d}:{s:02d}.{tenth}" if h else f"{m:02d}:{s:02d}.{tenth}")


_file_re = re.compile(r"[\w\-.()]+\.(?:" + "|".join(e[1:] for e in SOURCE_EXTS) + r")\b", re.I)


def extract_mentions(d, note):
    note = nfc(note)
    found = []
    for m in _file_re.finditer(note):
        if m.group(0) not in found:
            found.append(m.group(0))
    low = note.lower()
    have = {x.lower() for x in found}
    for s in list_sources(d):
        stem = os.path.splitext(s)[0]
        if len(stem) >= 5 and stem.lower() in low and s.lower() not in have:
            found.append(s)
            have.add(s.lower())
    return found


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def snapshots_dir(d, video_actual):
    return os.path.join(d.feedback, "snapshots", os.path.splitext(nfc(video_actual))[0])


def save_snapshot(d, video_actual, item_id, data_url):
    """화면 캡처(그림 포함)를 작업/feedback/snapshots/<영상>/<id>.jpg 로 저장"""
    m = re.match(r"data:(image/(?:jpeg|png));base64,(.+)$", data_url or "", re.S)
    if not m:
        return None
    raw = base64.b64decode(m.group(2))
    if len(raw) > 8 * 1024 * 1024:
        return None
    folder = snapshots_dir(d, video_actual)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, item_id + SNAP_TYPES[m.group(1)])
    with open(path, "wb") as f:
        f.write(raw)
    return os.path.relpath(path, d.feedback).replace(os.sep, "/")


def clean_drawing(raw):
    """화면에 그린 도형: 좌표는 영상 화면 기준 0~1 비율"""
    out = []
    if not isinstance(raw, list):
        return out
    for s in raw[:60]:
        if not isinstance(s, dict) or s.get("tool") not in ("pen", "arrow", "rect"):
            continue
        pts = []
        for p in (s.get("points") or [])[:3000]:
            try:
                pts.append([round(min(max(float(p[0]), 0.0), 1.0), 4), round(min(max(float(p[1]), 0.0), 1.0), 4)])
            except (TypeError, ValueError, IndexError):
                continue
        if len(pts) >= 2:
            color = s.get("color") if re.match(r"^#[0-9a-fA-F]{6}$", str(s.get("color", ""))) else "#ff3b30"
            out.append({"tool": s["tool"], "color": color, "points": pts})
    return out


def next_id(items, last=None):
    """f001, f002 … (지운 번호는 다시 쓰지 않음: last = 지금까지 쓴 가장 큰 번호)"""
    nums = [int(m.group(1)) for it in items for m in [re.fullmatch(r"f(\d+)", str(it.get("id", "")))] if m]
    m = re.fullmatch(r"f(\d+)", str(last or ""))
    return f"f{max(nums + ([int(m.group(1))] if m else []), default=0) + 1:03d}"


def _frame(body, key_frame, key_sec, fd):
    v = body.get(key_frame)
    if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
        return int(v)
    sec = body.get(key_sec)
    if sec is None:
        return None
    return int(round(Fraction(str(sec)) / fd))


def add_item(d, video_actual, body):
    memo = (body.get("memo") or body.get("note") or "").strip()
    if not memo:
        raise ValueError("메모 내용이 비어 있어요")
    tm = timing(d, video_actual)
    sf = max(0, _frame(body, "start_frame", "time", tm["fd"]) or 0)
    ef = _frame(body, "end_frame", "time_end", tm["fd"])
    if ef is not None and ef <= sf:
        ef = None
    t = float(sf * tm["fd"])
    tl = tm["tl"]
    seg = segment_at(tl, t + float(tm["fd"]) / 2)
    with file_lock(d):
        fb = load_feedback(d, video_actual)
        item = {"id": next_id(fb["items"], fb.get("last_id"))}
        fb["last_id"] = item["id"]
        _place(item, sf, ef, tm)
        ty = (body.get("type") or "기타").strip() or "기타"
        item.update({"type": LEGACY_TYPES.get(ty, ty), "memo": memo, "status": "todo", "claude_note": "", "resolved_in": None,
                     "source_clip": (seg["file"] or seg["name"] or "") if seg else "",
                     "subtitle": subtitle_at(tl, t + float(tm["fd"]) / 2),
                     "mentioned_files": extract_mentions(d, memo), "created_at": now_iso()})
        shapes = clean_drawing(body.get("drawing"))
        if shapes:
            item["drawing"] = shapes
        snap = save_snapshot(d, video_actual, item["id"], body.get("snapshot"))
        if snap:
            item["snapshot"] = snap  # 작업/feedback/ 기준 경로
        extra = body.get("_extra") or {}
        for k in ("remote_id", "origin", "author"):
            if extra.get(k):
                item[k] = extra[k]
        if seg:  # 반영할 때 클립을 정확히 찾기 위한 위치 정보
            item["lane"] = seg["lane"]
            if seg.get("clip_start") is not None:
                item["clip_range"] = [seg["clip_start"], seg["clip_end"]]
        fb.update(header_for(d, video_actual, tm))
        fb["items"].append(item)
        fb["items"].sort(key=lambda x: (x.get("start_frame", 0), x.get("id", "")))
        save_feedback(d, video_actual, fb)
    return ordered(item)


def update_item(d, video_actual, body):
    iid = body.get("id")
    with file_lock(d):
        fb = load_feedback(d, video_actual)
        for it in fb["items"]:
            if it.get("id") != iid:
                continue
            if "memo" in body or "note" in body:
                memo = (body.get("memo", body.get("note")) or "").strip()
                if not memo:
                    raise ValueError("메모 내용이 비어 있어요")
                it["memo"] = memo
                it["mentioned_files"] = extract_mentions(d, memo)
            if body.get("type"):
                it["type"] = body["type"]
            st = body.get("status")
            if st == "todo":  # 다시 요청
                it["status"] = "todo"
                it["claude_note"] = ""
                it["resolved_in"] = None
                it.pop("resolved_by", None)
            elif st == "done":  # 사용자가 직접 고치고 완료 표시
                it["status"] = "done"
                it["claude_note"] = (body.get("claude_note") or "직접 반영함").strip()
                it["resolved_by"] = "user"
            it["updated_at"] = now_iso()
            save_feedback(d, video_actual, fb)
            return ordered(it)
    raise KeyError("해당 피드백을 찾을 수 없어요")


def delete_item(d, video_actual, body):
    iid = body.get("id")
    with file_lock(d):
        fb = load_feedback(d, video_actual)
        target = next((it for it in fb["items"] if it.get("id") == iid), None)
        if target is None:
            raise KeyError("해당 피드백을 찾을 수 없어요")
        fb["items"] = [it for it in fb["items"] if it.get("id") != iid]
        save_feedback(d, video_actual, fb)
    snap = target.get("snapshot")
    if snap:
        base = os.path.realpath(os.path.join(d.feedback, "snapshots"))
        p = os.path.realpath(os.path.join(d.feedback, snap))
        if p.startswith(base + os.sep) and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass


def export_markers(d, video_actual, scope):
    """피드백을 리졸브 '타임라인 마커 EDL'로 저장 → 리졸브에서 마커로 가져오기"""
    tm = timing(d, video_actual)
    fb = load_feedback(d, video_actual)
    items = [i for i in fb["items"] if scope == "all" or i.get("status") == "todo"]
    if not items:
        raise ValueError("내보낼 피드백이 없어요")
    t0 = tm["tc0_frames"] if tm["tl"] else int(round(3600 / tm["fd"]))  # 타임라인이 없으면 리졸브 기본 01:00:00:00
    lines = [f"TITLE: {stem_of(video_actual)}", "FCM: NON-DROP FRAME", ""]
    for n, it in enumerate(items, 1):
        f_in = int(it.get("start_frame", 0))
        dur = max(1, int(it["end_frame"]) - f_in) if it.get("end_frame") is not None else 1
        tin, tout = tc_of(t0 + f_in, tm["base"]), tc_of(t0 + f_in + dur, tm["base"])
        name = " ".join(f"[{it.get('type', '기타')}] {it.get('memo', '')}".replace("|", "/").split())[:200]
        lines.append(f"{n:03d}  001      V     C        {tin} {tout} {tin} {tout}  ")
        lines.append(f" |C:{RESOLVE_COLORS.get(it.get('type'), 'ResolveColorPink')} |M:{name} |D:{dur}")
        lines.append("")
    folder = os.path.join(d.work, "markers")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, stem_of(video_actual) + "_markers.edl")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    tl = tm["tl"]
    return {"path": path, "rel": os.path.relpath(path, d.base), "count": len(items), "scope": scope,
            "drop_frame": bool(tl and tl.get("tc_format") == "DF"), "has_timeline": bool(tl)}


def feedback_counts(d, video_actual):
    try:
        items = load_feedback(d, video_actual)["items"]
    except Exception:
        return 0, 0
    return len(items), sum(1 for i in items if i.get("status") == "todo")


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FeedbackTool/1.0"
    dirs = None  # main()에서 설정
    jobs = None  # Claude Code 작업 관리 (claude_job.JobManager)
    port = 0

    def log_message(self, *args):
        pass

    # 공통 응답
    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, status, msg):
        self._json({"error": msg}, status)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost")

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        o = urlparse(origin)
        return (o.hostname or "").lower() in ("127.0.0.1", "localhost") and o.port == self.port

    # GET
    def do_GET(self):
        try:
            if not self._host_ok():
                return self._err(403, "허용되지 않은 접근이에요")
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = unquote(u.path)
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/api/state":
                return self._state()
            if path.startswith("/media/"):
                return self._media(path[len("/media/"):])
            if path.startswith("/snap/"):
                return self._snap(path[len("/snap/"):])
            if path == "/api/claude/job":
                return self._json(self.jobs.snapshot(int((q.get("after") or ["0"])[0] or 0)))
            if path == "/api/claude/info":
                return self._json(self.jobs.info())
            vname = (q.get("video") or [""])[0]
            if path in ("/api/timeline", "/api/feedback"):
                actual = resolve_video(self.dirs, vname)
                if not actual:
                    return self._err(404, "영상을 찾을 수 없어요")
                if path == "/api/timeline":
                    tl, name, err = get_timeline(self.dirs, actual)
                    return self._json({"timeline": name, "error": err, "data": tl,
                                       "production": production_for(self.dirs, actual)})
                remote.sync_background(self.dirs, actual, sys.modules[__name__])  # 고객 피드백 조용히 가져오기
                with LOCK:
                    fb = load_feedback(self.dirs, actual)
                fb.update(header_for(self.dirs, actual))
                return self._json(fb)
            self._err(404, "없는 주소예요")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._err(500, f"{type(e).__name__}: {e}")
            except Exception:
                pass

    # POST
    def do_POST(self):
        try:
            if not self._host_ok() or not self._origin_ok():
                return self._err(403, "허용되지 않은 접근이에요")
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._err(415, "JSON만 받아요")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._err(413, "내용이 너무 커요")
            body = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/claude/cancel":
                self.jobs.cancel()
                return self._json({"ok": True})
            if path == "/api/link/add":  # 구글 드라이브 영상 등록
                name = remote.add_link(self.dirs, body.get("url"), body.get("name"), body.get("fps"))
                return self._json({"ok": True, "video": name})
            if path == "/api/remote/setup":  # 고객 피드백 연결 (Apps Script 주소)
                return self._json(remote.setup(self.dirs, body.get("url"), body.get("key")))
            if path == "/api/remote/drivekey":  # 고객 페이지 재생용 드라이브 API 키
                info = remote.link_info(self.dirs, resolve_video(self.dirs, body.get("video")) or "") if body.get("video") else None
                return self._json(remote.set_drive_key(self.dirs, body.get("key"), (info or {}).get("drive_id")))
            if path == "/api/remote/pages":  # 고객 페이지 주소 (GitHub Pages)
                return self._json(remote.set_pages(self.dirs, body.get("url")))
            if path == "/api/remote/code":  # 구글에 붙여넣을 코드
                with open(os.path.join(HERE, "google", "Code.gs"), "r", encoding="utf-8") as f:
                    return self._json({"code": f.read()})
            body.pop("_extra", None)  # 원격 전용 필드는 페이지에서 못 넣게
            actual = resolve_video(self.dirs, body.get("video"))
            if not actual:
                return self._err(404, "영상을 찾을 수 없어요")
            if path == "/api/feedback/add":
                return self._json(add_item(self.dirs, actual, body))
            if path == "/api/feedback/update":
                return self._json(update_item(self.dirs, actual, body))
            if path == "/api/feedback/delete":
                delete_item(self.dirs, actual, body)
                return self._json({"ok": True})
            if path == "/api/claude/start":
                return self._json(self.jobs.start(actual, bool(body.get("continue", True))))
            if path == "/api/link/remove":
                remote.remove_link(self.dirs, actual)
                return self._json({"ok": True})
            if path == "/api/remote/share":
                return self._json(remote.share(self.dirs, actual, timing(self.dirs, actual)["fps"], stem_of(actual)))
            if path == "/api/remote/sync":
                return self._json(remote.sync(self.dirs, actual, sys.modules[__name__]))
            if path == "/api/export/markers":
                return self._json(export_markers(self.dirs, actual, "all" if body.get("scope") == "all" else "pending"))
            self._err(404, "없는 주소예요")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, KeyError) as e:
            self._err(400, str(e).strip("'\""))
        except remote.RemoteError as e:
            self._err(502, str(e))
        except Exception as e:
            try:
                self._err(500, f"{type(e).__name__}: {e}")
            except Exception:
                pass

    def _static(self, name, ctype):
        p = os.path.join(HERE, name)
        with open(p, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _snap(self, rel):
        base = os.path.realpath(os.path.join(self.dirs.feedback, "snapshots"))
        full = os.path.realpath(os.path.join(base, rel))
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            return self._err(404, "이미지를 찾을 수 없어요")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png" if full.lower().endswith(".png") else "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _state(self):
        d = self.dirs
        remote.import_background(d)  # 웹에서 연 영상 가져오기 (1분에 한 번)
        vids = []
        for v in sorted(list_video_files(d), key=lambda v: v["name"].lower()):
            tname = fcpxml.find_timeline(d.timeline, v["actual"])
            total, pending = feedback_counts(d, v["actual"])
            row = {"name": v["name"], "size": v["size"], "mtime": v["mtime"],
                   "timeline": nfc(tname) if tname else None, "total": total, "pending": pending}
            if v.get("url"):  # 구글 드라이브 영상
                info = remote.link_info(d, v["actual"]) or {}
                row.update({"url": v["url"], "drive_id": v["drive_id"], "fps": info.get("fps"),
                            "client_url": remote.client_url(d, info.get("token"))})
            vids.append(row)
        warn = []
        if not os.path.isdir(d.confirm):
            warn.append(f"컨펌본 폴더를 찾을 수 없어요: {d.confirm}")
        conf = remote.remote_conf(d)
        tls = sorted({os.path.splitext(nfc(f))[0] for f in fcpxml.list_timeline_files(d.timeline)}) if os.path.isdir(d.timeline) else []
        self._json({"project": nfc(os.path.basename(d.base)), "videos": vids, "tool_version": TOOL_VERSION,
                    "remote": {"configured": bool(conf.get("url") and conf.get("key")), "drive_key": bool(conf.get("drive_key")), "pages_url": conf.get("pages_url") or ""}, "timelines": tls,
                    "sources": list_sources(d), "warnings": warn})

    def _media(self, rel):
        actual = resolve_video(self.dirs, rel)
        if not actual:
            return self._err(404, "영상을 찾을 수 없어요")
        if remote.is_link(actual):  # 구글 드라이브 영상: 내 맥 서버가 받아서 그대로 넘겨줌 (구글이 다른 사이트의 직접 재생을 막아서)
            return self._drive(remote.link_info(self.dirs, actual))
        path = os.path.join(self.dirs.confirm, actual)
        size = os.path.getsize(path)
        ctype = "video/webm" if actual.lower().endswith(".webm") else "video/mp4"  # mov(H.264)도 mp4로 알려주면 크롬에서 잘 재생돼요
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and size > 0:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m:
                a, b = m.groups()
                if a == "" and b == "":
                    m = None
                elif a == "":
                    start = max(0, size - int(b))
                else:
                    start = int(a)
                    if b:
                        end = int(b)
                if m:
                    end = min(end, size - 1)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
        length = end - start + 1 if size > 0 else 0
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if length == 0:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 재생 중 이동(seek)하면 이전 요청이 끊기는 건 정상이에요


def _drive_stream(self, info):
    try:
        up, status = remote.open_drive(self.dirs, info["drive_id"], self.headers.get("Range"))
    except remote.RemoteError as e:
        return self._err(502, str(e))
    try:
        self.send_response(status)
        for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if up.headers.get(h):
                self.send_header(h, up.headers[h])
        if not up.headers.get("Accept-Ranges"):
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        while True:
            chunk = up.read(256 * 1024)
            if not chunk:
                break
            self.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass  # 재생 중 이동하면 이전 요청이 끊기는 건 정상
    finally:
        up.close()


Handler._drive = _drive_stream


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def main():
    ap = argparse.ArgumentParser(description="영상 편집 피드백 도구")
    ap.add_argument("--base", default=default_base(), help="컨펌본·타임라인·소스·작업 폴더가 들어 있는 기준 폴더")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않기")
    args = ap.parse_args()

    d = Dirs(os.path.abspath(os.path.expanduser(args.base)))
    if not os.path.isdir(d.base):
        sys.exit(f"기준 폴더를 찾을 수 없어요: {d.base}")
    os.makedirs(d.feedback, exist_ok=True)
    Handler.dirs = d
    Handler.jobs = claude_job.JobManager(d, load_feedback, production_for, list_video_files, nfc)

    srv = None
    for port in range(args.port, args.port + 20):
        try:
            srv = QuietServer(("127.0.0.1", port), Handler)  # 내 맥에서만 접속 가능
            Handler.port = port
            break
        except OSError:
            continue
    if srv is None:
        sys.exit(f"{args.port}번대 포트가 모두 사용 중이에요. --port 로 다른 번호를 지정해 주세요.")

    url = f"http://127.0.0.1:{Handler.port}/"
    info_path = os.path.join(d.feedback, ".server.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump({"port": Handler.port, "pid": os.getpid(), "url": url}, f)

    def _cleanup():
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(info_path)
        except (OSError, ValueError):
            pass
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # 종료 신호에도 정리하고 끄기
    print("=" * 56)
    print(" 영상 피드백 도구가 켜졌어요")
    print(f"  주소      {url}")
    print(f"  컨펌본    {d.confirm}")
    print(f"  피드백    {d.feedback}")
    print(" 끄려면 이 창에서 Ctrl + C")
    print("=" * 56)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료했어요.")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
