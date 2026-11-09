#!/usr/bin/env python3
"""FCPXML 편집 엔진 — 피드백을 리졸브 타임라인 수정으로 바꾸는 도구 (표준 라이브러리만)

Claude Code가 "피드백 반영"을 할 때 쓴다.
- 원본은 절대 덮어쓰지 않고 다음 버전 파일로 저장한다 (intro_v2.fcpxml → intro_v3.fcpxml)
- 시간은 모두 Fraction(유리수)로 계산하고 프레임 경계에 맞춘다 (부동소수점 금지)
- 컷 삭제·길이 조절 뒤에는 뒤 클립을 당기거나 밀어서(리플) 빈 구멍·겹침이 없게 한다
- 자신 없는 경우는 고치지 않고 이유를 돌려준다 (→ 피드백을 '질문'으로 남기면 됨)

명령
  info <fcpxml>                          주 스토리라인 클립 목록 (영상 기준 타임코드)
  locate <fcpxml> <시점>                 그 시점에 보이는 클립·연결 클립·자막
  apply <fcpxml> <plan.json> [--out 파일] [--dry-run]
                                         계획대로 고쳐 새 버전 저장 + 검증 결과(JSON) 출력
  validate <fcpxml> [--base 원본] [--expect-delta-frames N]
  next-name <fcpxml>                     다음 버전 파일 이름

시점 표기 (모두 '영상 시작 = 0' 기준)
  1234f  프레임 번호 · 00:00:41:05  타임코드(HH:MM:SS:FF) · 41.2  초

plan.json 예시
  {"ops": [
    {"id": "f001", "op": "cut",     "start": "00:00:12:00", "end": "00:00:14:15"},
    {"id": "f002", "op": "length",  "at": "00:00:20:00", "delta_frames": -15, "edge": "end"},
    {"id": "f003", "op": "move",    "clip": "00:00:30:00", "to": "00:00:05:00", "position": "before"},
    {"id": "f004", "op": "text",    "at": "00:00:08:00", "text": "새 자막", "old": "예전 자막 일부"},
    {"id": "f005", "op": "replace", "at": "00:00:09:00", "file": "소스/IMG_7474.MOV", "lane": "top"}
  ]}
"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from fractions import Fraction

# 타임라인에 놓이는 요소
CLIP_TAGS = {"asset-clip", "clip", "gap", "title", "video", "audio", "ref-clip", "mc-clip", "sync-clip", "caption"}
TIMED_TAGS = CLIP_TAGS | {"spine"}


class EditError(Exception):
    """자동으로 고치지 않는 게 안전한 경우 (이유는 사용자에게 그대로 보여줌)"""


def nfc(s):
    return unicodedata.normalize("NFC", s or "")


def T(s):
    """'1001/30000s' · '10s' → Fraction(초)"""
    if s is None:
        return Fraction(0)
    s = str(s).strip()
    if s.endswith("s"):
        s = s[:-1]
    if not s:
        return Fraction(0)
    if "/" in s:
        n, d = s.split("/", 1)
        return Fraction(int(n), int(d))
    return Fraction(s)


def ts(x):
    """Fraction(초) → FCPXML 시간 문자열"""
    x = Fraction(x)
    return f"{x.numerator}s" if x.denominator == 1 else f"{x.numerator}/{x.denominator}s"


def off(el):
    return T(el.get("offset"))


def dur(el):
    return T(el.get("duration"))


def start(el):
    return T(el.get("start")) if el.get("start") is not None else Fraction(0)


def src_basename(src):
    if not src:
        return None
    path = urllib.parse.unquote(urllib.parse.urlparse(src).path or src)
    return nfc(os.path.basename(path.rstrip("/"))) or None


class Timeline:
    def __init__(self, path):
        self.path = path
        real = os.path.join(path, "Info.fcpxml") if os.path.isdir(path) else path
        self.tree = ET.parse(real)
        self.root = self.tree.getroot()
        if self.root.tag != "fcpxml":
            raise EditError("FCPXML 파일이 아니에요")
        self.version = self.root.get("version", "")
        self.res = self.root.find("resources")
        self.seq = self.root.find(".//sequence")
        if self.seq is None or self.seq.find("spine") is None:
            raise EditError("타임라인(sequence/spine)을 찾지 못했어요")
        self.spine = self.seq.find("spine")
        fmt = self.by_id(self.seq.get("format"))
        if fmt is None or not fmt.get("frameDuration"):
            raise EditError("프레임 길이(frameDuration)를 찾지 못했어요")
        self.fd = T(fmt.get("frameDuration"))       # 1프레임 길이 (예: 1001/30000초)
        self.base = int(round(float(1 / self.fd)))  # 타임코드 프레임 자릿수 기준 (29.97 → 30)
        self.tc0 = T(self.seq.get("tcStart"))       # 타임라인 시작 타임코드 (리졸브는 보통 01:00:00:00)
        self.project = self.root.find(".//project")

    # ---------- 기본 정보 ----------
    def by_id(self, rid):
        if self.res is None or not rid:
            return None
        for el in self.res:
            if el.get("id") == rid:
                return el
        return None

    def asset_of(self, el):
        """클립이 쓰는 소스(asset) 요소"""
        a = self.by_id(el.get("ref"))
        if a is not None and a.tag == "asset":
            return a
        if el.tag == "clip":
            for ch in el:
                if ch.tag in ("video", "audio", "asset-clip") and ch.get("lane") is None:
                    a = self.by_id(ch.get("ref"))
                    if a is not None and a.tag == "asset":
                        return a
        return None

    def file_of(self, el):
        a = self.asset_of(el)
        if a is None:
            return None
        mr = a.find("media-rep")
        return src_basename(mr.get("src") if mr is not None else a.get("src"))

    def name_of(self, el):
        if el.tag in ("title", "caption"):
            t = self.text_of(el)
            return f"자막 \"{t}\"" if t else (el.get("name") or "자막")
        if el.tag == "gap":
            return "빈 구간(gap)"
        return self.file_of(el) or el.get("name") or el.tag

    @staticmethod
    def text_of(el):
        parts = ["".join(t.itertext()) for t in el.findall(".//text")]
        return " ".join(" ".join(parts).split())

    def primary(self):
        return [el for el in self.spine if el.tag in CLIP_TAGS]

    def end_abs(self):
        p = self.primary()
        return max((off(e) + dur(e) for e in p), default=self.tc0)

    def total_frames(self):
        return int((self.end_abs() - self.tc0) / self.fd)

    # ---------- 시점 변환 ----------
    def point(self, v):
        """'1234f' · 'HH:MM:SS:FF' · 초 → 영상 기준 프레임 번호"""
        if isinstance(v, int):
            return v
        s = str(v).strip()
        if s.endswith("f") and s[:-1].lstrip("-").isdigit():
            return int(s[:-1])
        if re.fullmatch(r"\d+:\d{2}:\d{2}[:;.]\d{2}", s):
            h, m, sec, f = map(int, re.split(r"[:;.]", s))
            return (h * 3600 + m * 60 + sec) * self.base + f
        try:
            return int(round(Fraction(s) / self.fd))
        except (ValueError, ZeroDivisionError):
            raise EditError(f"시점을 이해하지 못했어요: {v}")

    def abs_of(self, frame):
        return self.tc0 + Fraction(frame) * self.fd

    def frame_of(self, abs_t):
        return int(round((abs_t - self.tc0) / self.fd))

    def tc(self, frame, timeline=False):
        n = int(frame) + (int(round(self.tc0 / self.fd)) if timeline else 0)
        f = n % self.base
        s = n // self.base
        h, r = divmod(s, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

    # ---------- 모든 요소의 절대 위치 ----------
    def walk(self):
        """[{el, a, b, lane, parent}] — a/b는 spine 기준 절대 시간"""
        out = []

        def rec(parent, abs_origin, local_origin, lane, plain):
            for el in parent:
                if el.tag not in TIMED_TAGS:
                    continue
                if not plain and el.get("lane") is None and el.tag not in ("title", "caption", "spine"):
                    continue  # <clip> 안의 본체 video/audio
                o = off(el) if el.get("offset") is not None else local_origin
                a = abs_origin + (o - local_origin)
                ln = lane + int(el.get("lane", 0))
                if el.tag == "spine":
                    rec(el, a, Fraction(0), ln, True)
                    continue
                out.append({"el": el, "a": a, "b": a + dur(el), "lane": ln, "parent": parent})
                rec(el, a, start(el), ln, False)
        rec(self.spine, Fraction(0), Fraction(0), 0, True)
        return out

    def primary_at(self, frame):
        t = self.abs_of(frame) + self.fd / 2  # 프레임 가운데
        for el in self.primary():
            if off(el) <= t < off(el) + dur(el):
                return el
        raise EditError(f"{self.tc(frame)} 지점에 클립이 없어요 (타임라인 길이 {self.tc(self.total_frames())})")

    def contains(self, el):
        return any(x is el for x in self.root.iter())

    def has_transition_near(self, a, b):
        for el in self.spine:
            if el.tag == "transition":
                o, d = off(el), dur(el)
                if o <= b and o + d >= a:
                    return True
        return False

    def set_seq_duration(self):
        self.seq.set("duration", ts(self.end_abs() - self.tc0))

    # ---------- 컷 삭제 (리플) ----------
    def cut(self, fa, fb):
        """영상 기준 프레임 fa~fb(fb 미포함)를 잘라내고 뒤를 당김"""
        if fb <= fa:
            raise EditError("구간 끝이 시작보다 앞이에요")
        A, B = self.abs_of(fa), self.abs_of(fb)
        L = B - A
        if fa < 0 or B > self.end_abs():
            raise EditError("구간이 타임라인 밖이에요")
        if self.has_transition_near(A, B):
            raise EditError("잘라낼 구간에 장면 전환(트랜지션)이 걸쳐 있어서 자동으로 자르지 않았어요. 리졸브에서 직접 잘라 주세요")
        notes, new_list, orphans = [], [], []
        for el in list(self.spine):
            if el.tag not in CLIP_TAGS:
                new_list.append(el)
                continue
            O, D, S = off(el), dur(el), start(el)
            E = O + D
            if E <= A or O >= B:  # 클립 자체는 잘린 구간과 상관없음
                if E <= A:  # 그래도 이 클립에 붙어 길게 이어지는 연결 클립(배경음악 등)은 잘린 구간만큼 뺌
                    orphans += self._fix_children(el, O, S, A, B, notes, "left")
                new_list.append(el)
                continue
            nm = self.name_of(el)
            if O >= A and E <= B:  # 통째로 잘린 구간 안
                orphans += self._fix_children(el, O, S, A, B, notes, "left")
                notes.append(f"'{nm}' 통째로 삭제")
                continue
            if O < A and E > B:  # 클립 가운데를 잘라 둘로 나눔
                right = copy.deepcopy(el)
                el.set("duration", ts(A - O))
                right.set("offset", ts(B))
                right.set("start", ts(S + (B - O)))
                right.set("duration", ts(E - B))
                self._fix_children(el, O, S, A, B, notes, "left", has_right_piece=True)
                self._fix_children(right, O, S, A, B, notes, "right")
                new_list += [el, right]
                notes.append(f"'{nm}' 가운데 {self._len(L)} 삭제")
            elif O < A:  # 클립 뒷부분 삭제
                el.set("duration", ts(A - O))
                orphans += self._fix_children(el, O, S, A, B, notes, "left")
                new_list.append(el)
                notes.append(f"'{nm}' 뒷부분 {self._len(E - A)} 삭제")
            else:  # 클립 앞부분 삭제
                el.set("offset", ts(B))
                el.set("start", ts(S + (B - O)))
                el.set("duration", ts(E - B))
                self._fix_children(el, O, S, A, B, notes, "right")
                new_list.append(el)
                notes.append(f"'{nm}' 앞부분 {self._len(B - O)} 삭제")
        self._rebuild_spine(new_list)
        self._reattach(orphans, B, notes)
        for el in self.spine:  # 리플: 뒤 요소를 잘라낸 길이만큼 앞으로
            if el.get("offset") is not None and off(el) >= B:
                el.set("offset", ts(off(el) - L))
        self.set_seq_duration()
        return notes, -int(L / self.fd)

    def _fix_children(self, parent, O, S, A, B, notes, keep, has_right_piece=False):
        """잘린 부모 클립에 붙은 연결 클립(B롤·자막·배경음악) 정리.
        O, S: 부모의 원래 위치·소스 시작 / A~B: 잘라낸 구간 / keep: 이 부모 조각이 남는 쪽(left·right)
        has_right_piece: 같은 부모의 오른쪽 조각이 따로 있음(클립 가운데를 자른 경우)
        돌려주는 값: 잘린 지점 뒤 클립에 다시 붙여야 하는 뒷조각들"""
        orphans = []
        for c in list(parent):
            if c.tag not in TIMED_TAGS or c.get("lane") is None:
                continue
            cs = O + (off(c) - S)  # 원래 절대 위치
            ce = cs + dur(c)
            nm = self.name_of(c)
            if ce <= A:
                side = "left"
            elif cs >= B:
                side = "right"
            elif cs >= A and ce <= B:  # 잘린 구간 안에 통째로 있음 → 삭제
                parent.remove(c)
                msg = f"연결된 '{nm}' 삭제 (잘린 구간 안)"
                if msg not in notes:
                    notes.append(msg)
                continue
            elif cs < A and keep == "right" and ce > B:  # 잘린 구간을 가로지름(배경음악 등) → 오른쪽 조각에도 뒷부분을 남김
                c.set("start", ts(start(c) + (B - cs)))
                c.set("offset", ts(S + (B - O)))
                c.set("duration", ts(ce - B))
                side = "right"
            elif cs < A:  # 앞에서 시작해 잘린 구간에 걸침
                side = "left"
                if keep == "left":
                    if ce > B and not has_right_piece:  # 잘린 구간 뒤로도 이어짐(배경음악 등) → 뒷조각을 떼어 다시 붙임
                        piece = copy.deepcopy(c)
                        piece.set("start", ts(start(c) + (B - cs)))
                        piece.set("duration", ts(ce - B))
                        orphans.append(piece)
                    c.set("duration", ts(A - cs))
                    notes.append(f"연결된 '{nm}'에서 잘린 구간만큼 뺌")
            else:  # 잘린 구간 안에서 시작해 뒤로 이어짐
                side = "right"
                if keep == "left" and not has_right_piece:  # 이 부모에는 붙일 자리가 없음 → 뒤 클립에 붙임
                    c.set("start", ts(start(c) + (B - cs)))
                    c.set("duration", ts(ce - B))
                    parent.remove(c)
                    orphans.append(c)
                    notes.append(f"연결된 '{nm}' 앞부분을 잘라 뒤 클립에 붙임")
                    continue
                if keep == "right":
                    c.set("start", ts(start(c) + (B - cs)))
                    c.set("offset", ts(S + (B - O)))
                    c.set("duration", ts(ce - B))
                    notes.append(f"연결된 '{nm}' 앞부분을 잘라 이어 붙임")
            if side != keep:  # 반대쪽 조각에 속함
                parent.remove(c)
                continue
            if not (start(parent) <= off(c) <= start(parent) + dur(parent)):  # 연결 지점이 부모 범위 안이어야 함
                raise EditError(f"연결 클립 '{nm}'의 붙는 위치가 잘린 구간과 겹쳐서 자동으로 처리하지 않았어요")
        return orphans

    def _reattach(self, orphans, B, notes):
        """잘린 지점(B) 바로 뒤 클립에 뒷조각을 연결 클립으로 다시 붙임 (리플 전 좌표 기준)"""
        for piece in orphans:
            host = next((el for el in self.spine if el.tag in CLIP_TAGS and off(el) <= B < off(el) + dur(el)), None)
            if host is None:
                notes.append(f"'{self.name_of(piece)}' 뒷부분은 타임라인 끝이라 없앰")
                continue
            piece.set("offset", ts(start(host) + (B - off(host))))
            piece.tail = "\n"
            host.append(piece)

    def _rebuild_spine(self, new_list):
        tails = [el.tail for el in self.spine]
        default_tail = tails[0] if tails else "\n"
        for el in list(self.spine):
            self.spine.remove(el)
        for el in new_list:
            if el.tail is None or not el.tail.strip() == "":
                el.tail = default_tail
            self.spine.append(el)

    def _len(self, x):
        n = int(round(Fraction(x) / self.fd))
        return f"{n}프레임({float(Fraction(x)):.2f}초)"

    # ---------- 길이 조절 ----------
    def media_room(self, el, edge):
        """원본 소스에서 더 늘릴 수 있는 길이 (None = 알 수 없음)"""
        if el.tag in ("gap", "title", "caption") or (el.tag == "video" and self.asset_of(el) is None):
            return Fraction(10 ** 6)  # 만든 요소는 제한 없음
        a = self.asset_of(el)
        if a is None or a.get("duration") is None:
            return None
        a_start, a_dur = start(a), dur(a)
        if edge == "end":
            return (a_start + a_dur) - (start(el) + dur(el))
        return start(el) - a_start

    def length(self, el, delta_frames, edge="end"):
        if delta_frames == 0:
            raise EditError("바꿀 길이가 0이에요")
        O, D = off(el), dur(el)
        dfr = int(round(D / self.fd))
        nm = self.name_of(el)
        if delta_frames < 0:
            if -delta_frames >= dfr:
                raise EditError(f"'{nm}'는 {dfr}프레임이라 {-delta_frames}프레임만큼 줄일 수 없어요 (통째로 빼려면 컷 삭제로)")
            f_end = self.frame_of(O + D)
            f_start = self.frame_of(O)
            if edge == "end":
                notes, dl = self.cut(f_end + delta_frames, f_end)
            else:
                notes, dl = self.cut(f_start, f_start - delta_frames)
            return [f"'{nm}' {'끝' if edge == 'end' else '앞'}을 {-delta_frames}프레임 줄임"], dl
        need = Fraction(delta_frames) * self.fd
        room = self.media_room(el, edge)
        if room is None:
            raise EditError(f"'{nm}' 원본 소스 길이를 알 수 없어서 늘리지 않았어요")
        if room < need:
            raise EditError(f"'{nm}' 원본 소스가 {self._len(max(room, Fraction(0)))}밖에 남지 않아 {delta_frames}프레임 늘릴 수 없어요")
        point = O + D if edge == "end" else O
        if self.has_transition_near(point - self.fd, point + self.fd):
            raise EditError("늘릴 지점에 장면 전환(트랜지션)이 있어서 자동으로 늘리지 않았어요")
        if edge == "start":
            el.set("start", ts(start(el) - need))
        el.set("duration", ts(D + need))
        for x in self.spine:  # 리플: 뒤 요소를 늘린 길이만큼 뒤로
            if x is not el and x.get("offset") is not None and off(x) >= O + D:
                x.set("offset", ts(off(x) + need))
        self.set_seq_duration()
        return [f"'{nm}' {'끝' if edge == 'end' else '앞'}을 {delta_frames}프레임 늘림"], delta_frames

    # ---------- 순서 변경 ----------
    def move(self, el, target, position="before"):
        if any(x.tag == "transition" for x in self.spine):
            raise EditError("타임라인에 장면 전환(트랜지션)이 있어서 순서 변경은 리졸브에서 직접 해 주세요")
        if el is target:
            raise EditError("옮길 클립과 기준 클립이 같아요")
        items = list(self.spine)
        first = min(off(x) for x in items if x.tag in CLIP_TAGS)
        items.remove(el)
        i = items.index(target)
        items.insert(i if position == "before" else i + 1, el)
        self._rebuild_spine(items)
        run = first
        for x in self.spine:  # 앞에서부터 차례로 다시 배치 (빈 구멍·겹침 없음)
            if x.tag in CLIP_TAGS:
                x.set("offset", ts(run))
                run += dur(x)
        self.set_seq_duration()
        return [f"'{self.name_of(el)}'를 '{self.name_of(target)}' {'앞' if position == 'before' else '뒤'}으로 옮김"], 0

    # ---------- 자막 수정 ----------
    def titles_at(self, frame):
        t = self.abs_of(frame) + self.fd / 2
        return [w for w in self.walk() if w["el"].tag in ("title", "caption") and w["a"] <= t < w["b"]]

    def text(self, frame, new, old=None):
        cands = self.titles_at(frame)
        if old:
            cands = [w for w in cands if nfc(old).replace(" ", "") in nfc(self.text_of(w["el"])).replace(" ", "")]
        if not cands:
            raise EditError(f"{self.tc(frame)}에 " + ("그 문구가 들어간 " if old else "") + "자막(제목 클립)이 없어요")
        if len(cands) > 1:
            top = max(w["lane"] for w in cands)
            cands = [w for w in cands if w["lane"] == top]
            if len(cands) > 1:
                raise EditError("그 시점에 자막이 여러 개 겹쳐 있어요. 바꿀 자막의 지금 문구를 알려 주세요")
        el = cands[0]["el"]
        txt = el.find("text")
        if txt is None:
            raise EditError("이 자막은 텍스트 구조가 달라서 자동으로 고치지 않았어요")
        before = self.text_of(el)
        styles = txt.findall("text-style")
        if styles:
            styles[0].text = new
            for s in styles[1:]:
                txt.remove(s)
            styles[0].tail = None
            txt.text = None
        else:
            txt.text = new
        return el, [f"자막 \"{before}\" → \"{new}\""], 0

    # ---------- 클립 교체 ----------
    def clip_at(self, frame, lane="top"):
        t = self.abs_of(frame) + self.fd / 2
        cands = [w for w in self.walk() if w["el"].tag in ("asset-clip", "clip", "video", "ref-clip", "mc-clip", "sync-clip")
                 and w["a"] <= t < w["b"] and (self.asset_of(w["el"]) is None or self.asset_of(w["el"]).get("hasVideo") != "0")]
        if lane not in ("top", None):
            cands = [w for w in cands if w["lane"] == int(lane)]
        if not cands:
            raise EditError(f"{self.tc(frame)}에 바꿀 영상 클립이 없어요")
        return max(cands, key=lambda w: w["lane"])

    def replace(self, frame, file_path, lane="top", src_in=None):
        w = self.clip_at(frame, lane)
        el = w["el"]
        if el.tag != "asset-clip":
            raise EditError(f"'{self.name_of(el)}'는 {el.tag} 형식이라 자동으로 바꾸지 않았어요 (asset-clip만 지원)")
        if not os.path.isfile(file_path):
            raise EditError(f"소스 파일이 없어요: {file_path}")
        fname = nfc(os.path.basename(file_path))
        asset = None
        for a in (self.res if self.res is not None else []):
            if a.tag == "asset":
                mr = a.find("media-rep")
                if src_basename(mr.get("src") if mr is not None else a.get("src")) == fname:
                    asset = a
                    break
        if asset is None:
            asset = self._new_asset(file_path, el)
        a_dur = dur(asset)
        need = dur(el)
        s_in = Fraction(0) if src_in is None else Fraction(int(round(Fraction(str(src_in)) / self.fd))) * self.fd
        if a_dur and s_in + need > start(asset) + a_dur:
            raise EditError(f"'{fname}' 길이({float(a_dur):.2f}초)가 바꿀 구간({float(need):.2f}초)보다 짧아요")
        before = self.name_of(el)
        el.set("ref", asset.get("id"))
        el.set("name", os.path.splitext(fname)[0])
        el.set("start", ts(start(asset) + s_in))
        return el, [f"'{before}' → '{fname}' 교체 ({self.tc(self.frame_of(w['a']))}~{self.tc(self.frame_of(w['b']))})"], 0

    def _new_asset(self, file_path, like_el):
        secs = probe_duration(file_path)
        if not secs:
            raise EditError(f"'{os.path.basename(file_path)}' 길이를 알아낼 수 없어서 교체하지 않았어요 (ffprobe 필요)")
        ids = [int(m.group(1)) for a in self.res for m in [re.fullmatch(r"r(\d+)", a.get("id") or "")] if m]
        rid = f"r{max(ids, default=0) + 1}"
        frames = int(Fraction(str(secs)) / self.fd)  # 프레임 단위로 내림
        old = self.asset_of(like_el)
        attrs = {"id": rid, "name": os.path.splitext(nfc(os.path.basename(file_path)))[0], "start": "0s",
                 "duration": ts(frames * self.fd), "hasVideo": "1", "hasAudio": "1",
                 "format": (old.get("format") if old is not None and old.get("format") else self.seq.get("format"))}
        url = "file://" + urllib.parse.quote(os.path.abspath(file_path))
        a = ET.Element("asset", attrs)
        if self._ver() >= (1, 9):
            ET.SubElement(a, "media-rep", {"kind": "original-media", "src": url})
        else:
            a.set("src", url)
        a.tail = "\n"
        # format 앞이 아닌 마지막 asset 뒤에 넣기
        last = max((i for i, x in enumerate(self.res) if x.tag == "asset"), default=len(self.res) - 1)
        self.res.insert(last + 1, a)
        return a

    def _ver(self):
        try:
            return tuple(int(x) for x in self.version.split(".")[:2])
        except ValueError:
            return (1, 9)

    # ---------- 저장 ----------
    def dedupe_ids(self):
        """복사로 생긴 중복 id(자막 스타일 등)를 고유하게 바꾸고, 같은 조각 안의 참조도 함께 바꿈"""
        seen = set()
        for el in self.root.iter():
            i = el.get("id")
            if not i:
                continue
            if i not in seen:
                seen.add(i)
                continue
            # el 이 속한 가장 가까운 클립 조각을 찾아 그 안에서만 바꿈
            holder = next((w["el"] for w in self.walk() if any(x is el for x in w["el"].iter())), None)
            n = 2
            while f"{i}_{n}" in seen:
                n += 1
            new = f"{i}_{n}"
            seen.add(new)
            scope = holder if holder is not None else el
            for x in scope.iter():
                if x.get("id") == i:
                    x.set("id", new)
                if x.get("ref") == i:
                    x.set("ref", new)

    def save(self, out_path, new_name=None):
        self.dedupe_ids()
        if new_name and self.project is not None:
            self.project.set("name", new_name)
        body = ET.tostring(self.root, encoding="unicode")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n' + body + "\n")


def probe_duration(path):
    """영상 길이(초). ffprobe → mdls(맥 기본) 순서로 시도"""
    ff = shutil.which("ffprobe")
    if ff:
        r = subprocess.run([ff, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True)
        try:
            return float(r.stdout.strip())
        except ValueError:
            pass
    md = shutil.which("mdls")
    if md:
        r = subprocess.run([md, "-name", "kMDItemDurationSeconds", "-raw", path], capture_output=True, text=True)
        try:
            return float(r.stdout.strip())
        except ValueError:
            pass
    return None


def next_name(path):
    folder = os.path.dirname(os.path.abspath(path))
    stem = os.path.splitext(os.path.basename(path.rstrip("/")))[0]
    m = re.search(r"_v(\d+)$", stem)
    base, n = (stem[: m.start()], int(m.group(1)) + 1) if m else (stem, 2)
    while os.path.exists(os.path.join(folder, f"{base}_v{n}.fcpxml")):
        n += 1
    return os.path.join(folder, f"{base}_v{n}.fcpxml")


# ---------- 계획 실행 ----------
def apply_plan(tl, ops):
    results = {}
    order = []

    def fail(op, msg):
        results[op.get("id") or f"#{len(results) + 1}"] = {"id": op.get("id"), "op": op.get("op"), "ok": False, "message": msg, "delta_frames": 0}

    prepared = []
    # 1) 먼저 원래 타임라인 기준으로 대상을 모두 정해 둠 (다른 수정에 밀려 엇갈리지 않게)
    for op in ops:
        try:
            kind = op.get("op")
            if kind == "cut":
                fa, fb = tl.point(op["start"]), tl.point(op["end"])
                prepared.append((op, kind, {"fa": fa, "fb": fb, "span": (fa, fb)}))
            elif kind == "length":
                el = tl.primary_at(tl.point(op["at"]))
                d = int(op["delta_frames"])
                edge = op.get("edge", "end")
                f0, f1 = tl.frame_of(off(el)), tl.frame_of(off(el) + dur(el))
                span = (f1 + d, f1) if (d < 0 and edge == "end") else (f0, f0 - d) if d < 0 else (f0, f1)
                prepared.append((op, kind, {"el": el, "d": d, "edge": edge, "span": span}))
            elif kind == "move":
                el = tl.primary_at(tl.point(op["clip"]))
                tgt = tl.primary_at(tl.point(op["to"]))
                prepared.append((op, kind, {"el": el, "tgt": tgt, "pos": op.get("position", "before")}))
            elif kind == "text":
                if not op.get("text"):
                    raise EditError("새 자막 문구가 비어 있어요")
                prepared.append((op, kind, {"frame": tl.point(op["at"]), "text": op["text"], "old": op.get("old")}))
            elif kind == "replace":
                prepared.append((op, kind, {"frame": tl.point(op["at"]), "file": op["file"], "lane": op.get("lane", "top"), "src_in": op.get("src_in")}))
            else:
                raise EditError(f"모르는 작업 종류예요: {kind}")
        except (EditError, KeyError, ValueError) as e:
            fail(op, str(e) if isinstance(e, EditError) else f"계획 형식 오류: {e}")
    # 겹치는 시간 수정은 함께 처리하지 않음
    timed = [p for p in prepared if p[1] in ("cut", "length")]
    for i, (op, kind, a) in enumerate(timed):
        for op2, kind2, b in timed[:i]:
            if a["span"][0] < b["span"][1] and b["span"][0] < a["span"][1]:
                fail(op, f"다른 피드백({op2.get('id')})과 같은 구간이라 함께 처리하지 않았어요")
                break
    done_ids = set(results)
    alive = [p for p in prepared if (p[0].get("id") or "") not in done_ids]

    def ok(op, notes, delta, el=None):
        results[op.get("id") or f"#{len(results) + 1}"] = {"id": op.get("id"), "op": op.get("op"), "ok": True,
                                                         "message": "; ".join(notes), "delta_frames": delta, "_el": el}

    # 2) 길이가 안 바뀌는 수정 (자막·교체)
    for op, kind, a in alive:
        try:
            if kind == "text":
                el, notes, d = tl.text(a["frame"], a["text"], a["old"])
                ok(op, notes, d, el)
            elif kind == "replace":
                el, notes, d = tl.replace(a["frame"], a["file"], a["lane"], a["src_in"])
                ok(op, notes, d, el)
        except EditError as e:
            fail(op, str(e))
    # 3) 길이가 바뀌는 수정: 뒤쪽부터 (앞쪽 시점이 밀리지 않게)
    timed_alive = [p for p in alive if p[1] in ("cut", "length")]
    timed_alive.sort(key=lambda p: p[2]["span"][0], reverse=True)
    for op, kind, a in timed_alive:
        try:
            if kind == "cut":
                notes, d = tl.cut(a["fa"], a["fb"])
            else:
                if not tl.contains(a["el"]):
                    raise EditError("다른 수정으로 이 클립이 없어졌어요")
                notes, d = tl.length(a["el"], a["d"], a["edge"])
            ok(op, notes, d)
        except EditError as e:
            fail(op, str(e))
    # 4) 순서 변경
    for op, kind, a in alive:
        if kind != "move":
            continue
        try:
            if not (any(x is a["el"] for x in tl.spine) and any(x is a["tgt"] for x in tl.spine)):
                raise EditError("다른 수정(컷)으로 옮길 클립이 바뀌어서 순서 변경을 하지 않았어요")
            notes, d = tl.move(a["el"], a["tgt"], a["pos"])
            ok(op, notes, d)
        except EditError as e:
            fail(op, str(e))
    # 자막·교체 대상이 컷으로 사라졌으면 알림
    for r in results.values():
        el = r.pop("_el", None)
        if r["ok"] and el is not None and not tl.contains(el):
            r["ok"] = False
            r["message"] = "삭제된 구간 안에 있어서 결과에 남지 않아요 (다른 피드백의 컷과 겹침)"
            r["delta_frames"] = 0
    # 원래 순서대로
    out = []
    for op in ops:
        r = results.get(op.get("id"))
        if r:
            out.append(r)
    return out


# ---------- 검증 ----------
def validate(path, base=None, expect_delta=None):
    rep = {"ok": True, "checks": []}

    def check(name, good, detail=""):
        rep["checks"].append({"name": name, "ok": bool(good), "detail": detail})
        if not good:
            rep["ok"] = False

    xl = shutil.which("xmllint")
    if xl:
        r = subprocess.run([xl, "--noout", path], capture_output=True, text=True)
        check("XML 문법 (xmllint)", r.returncode == 0, r.stderr.strip()[:300])
    try:
        tl = Timeline(path)
    except Exception as e:
        check("FCPXML 읽기", False, str(e))
        return rep
    if not xl:
        check("XML 문법 (파이썬 파서)", True, "xmllint가 없어 파이썬으로 확인")
    ids = {el.get("id") for el in tl.root.iter() if el.get("id")}
    missing = sorted({el.get("ref") for el in tl.root.iter() if el.get("ref") and el.get("ref") not in ids})
    check("참조(ref)가 모두 있음 (소스·효과·자막 스타일)", not missing, ", ".join(missing))
    all_ids = [el.get("id") for el in tl.root.iter() if el.get("id")]
    dup = sorted({i for i in all_ids if all_ids.count(i) > 1})
    check("id가 겹치지 않음", not dup, ", ".join(dup))
    # 주 스토리라인: 빈 구멍·겹침
    prim = tl.primary()
    holes = []
    for a, b in zip(prim, prim[1:]):
        if off(a) + dur(a) != off(b):
            holes.append(f"{tl.tc(tl.frame_of(off(a) + dur(a)))} ↔ {tl.tc(tl.frame_of(off(b)))}")
    if any(x.tag == "transition" for x in tl.spine):
        check("클립 사이 빈 구멍·겹침 없음", True, "장면 전환이 있어 이 검사는 건너뜀")
    else:
        check("클립 사이 빈 구멍·겹침 없음", not holes, "; ".join(holes[:5]))
    if prim:
        check("첫 클립이 타임라인 시작에 있음", off(prim[0]) == tl.tc0, f"{tl.tc(tl.frame_of(off(prim[0])))}")
    # 프레임 경계
    bad = []
    for w in tl.walk():
        el = w["el"]
        if (w["a"] - tl.tc0) % tl.fd or dur(el) % tl.fd:
            bad.append(tl.name_of(el))
    check("모든 위치·길이가 프레임 경계에 맞음", not bad, ", ".join(bad[:5]))
    seq_d = dur(tl.seq)
    check("시퀀스 길이 = 클립 길이 합", seq_d == tl.end_abs() - tl.tc0, f"{float(seq_d):.3f}초 / {float(tl.end_abs() - tl.tc0):.3f}초")
    # 소스 파일 존재 (맥에서 실행할 때만 의미 있음)
    miss_files = []
    for a in (tl.res if tl.res is not None else []):
        if a.tag != "asset":
            continue
        mr = a.find("media-rep")
        src = mr.get("src") if mr is not None else a.get("src")
        if src and src.startswith("file://"):
            p = urllib.parse.unquote(urllib.parse.urlparse(src).path)
            if not os.path.exists(p):
                miss_files.append(os.path.basename(p))
    rep["missing_media"] = miss_files
    rep["total_frames"] = tl.total_frames()
    rep["total_tc"] = tl.tc(tl.total_frames())
    if base:
        b = Timeline(base)
        delta = tl.total_frames() - b.total_frames()
        rep["delta_frames"] = delta
        if expect_delta is not None:
            check("총 길이 변화 = 수정한 길이", delta == expect_delta,
                  f"원본 {b.tc(b.total_frames())} → 새 {tl.tc(tl.total_frames())} (변화 {delta}프레임, 예상 {expect_delta}프레임)")
    return rep


# ---------- 명령 ----------
def cmd_info(path):
    tl = Timeline(path)
    print(f"FCPXML {tl.version} · {float(1 / tl.fd):.3f}fps (frameDuration {ts(tl.fd)}) · 시작 TC {tl.tc(0, True)} · 길이 {tl.tc(tl.total_frames())} ({tl.total_frames()}프레임)")
    fmt = tl.by_id(tl.seq.get("format"))
    if fmt is not None:
        print(f"해상도 {fmt.get('width')}x{fmt.get('height')} · 프로젝트 '{tl.project.get('name') if tl.project is not None else ''}'")
    if any(x.tag == "transition" for x in tl.spine):
        print("⚠ 장면 전환(트랜지션)이 있어요 — 그 근처 컷·순서 변경은 자동으로 하지 않아요")
    print("\n# 주 스토리라인 (영상 기준 타임코드)")
    allw = tl.walk()
    for i, el in enumerate(tl.primary(), 1):
        a, b = tl.frame_of(off(el)), tl.frame_of(off(el) + dur(el))
        print(f"{i:>3}. {tl.tc(a)} ~ {tl.tc(b)}  ({b - a}f)  {el.tag:<10} {tl.name_of(el)}")
        for w in allw:
            if w["parent"] is el:
                print(f"       └ lane {w['lane']:>2}  {tl.tc(tl.frame_of(w['a']))} ~ {tl.tc(tl.frame_of(w['b']))}  {tl.name_of(w['el'])}")


def cmd_locate(path, point):
    tl = Timeline(path)
    f = tl.point(point)
    t = tl.abs_of(f) + tl.fd / 2
    print(f"{tl.tc(f)} (프레임 {f}, 타임라인 {tl.tc(f, True)})")
    for w in sorted(tl.walk(), key=lambda w: w["lane"]):
        if w["a"] <= t < w["b"]:
            print(f"  lane {w['lane']:>2}  {w['el'].tag:<10} {tl.tc(tl.frame_of(w['a']))} ~ {tl.tc(tl.frame_of(w['b']))}  {tl.name_of(w['el'])}")


def cmd_apply(path, plan_path, out=None, dry=False):
    plan = json.load(sys.stdin if plan_path == "-" else open(plan_path, encoding="utf-8"))
    ops = plan.get("ops", plan if isinstance(plan, list) else [])
    tl = Timeline(path)
    before = tl.total_frames()
    results = apply_plan(tl, ops)
    n_ok = sum(r["ok"] for r in results)
    report = {"source": path, "out": None, "results": results, "applied": n_ok, "failed": len(results) - n_ok,
              "total_before": tl.tc(before), "total_after": tl.tc(tl.total_frames()), "delta_frames": tl.total_frames() - before}
    if n_ok and not dry:
        out = out or next_name(path)
        if os.path.abspath(out) == os.path.abspath(path) or os.path.exists(out):
            raise SystemExit("✗ 원본이나 이미 있는 파일은 덮어쓰지 않아요: " + out)
        tl.save(out, os.path.splitext(os.path.basename(out))[0])
        report["out"] = out
        report["validation"] = validate(out, path, sum(r["delta_frames"] for r in results if r["ok"]))
    print(json.dumps(report, ensure_ascii=False, indent=1))


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, args = argv[1], argv[2:]
    try:
        if cmd == "info":
            cmd_info(args[0])
        elif cmd == "locate":
            cmd_locate(args[0], args[1])
        elif cmd == "apply":
            out = args[args.index("--out") + 1] if "--out" in args else None
            cmd_apply(args[0], args[1], out, "--dry-run" in args)
        elif cmd == "validate":
            base = args[args.index("--base") + 1] if "--base" in args else None
            exp = int(args[args.index("--expect-delta-frames") + 1]) if "--expect-delta-frames" in args else None
            rep = validate(args[0], base, exp)
            for c in rep["checks"]:
                print(("✓ " if c["ok"] else "✗ ") + c["name"] + (f" — {c['detail']}" if c["detail"] else ""))
            print(f"길이 {rep.get('total_tc')}" + (f" · 변화 {rep['delta_frames']}프레임" if "delta_frames" in rep else ""))
            if rep.get("missing_media"):
                print("⚠ 이 컴퓨터에서 찾을 수 없는 소스: " + ", ".join(rep["missing_media"]))
            sys.exit(0 if rep["ok"] else 1)
        elif cmd == "next-name":
            print(next_name(args[0]))
        else:
            print(__doc__)
    except EditError as e:
        print("✗ " + str(e), file=sys.stderr)
        sys.exit(2)
    except IndexError:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
