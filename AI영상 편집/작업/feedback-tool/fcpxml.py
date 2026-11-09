#!/usr/bin/env python3
"""FCPXML(다빈치 리졸브·파이널컷에서 내보낸 타임라인)에서 화면에 필요한 정보만 읽는 모듈.

- 표준 라이브러리만 사용 (추가 설치 없음)
- 읽는 것: 프레임레이트, 클립 구간·소스 파일명, 자막(title/caption) 텍스트
- 확인 가능한 형식: FCPXML 1.6 ~ 1.11 (.fcpxml 파일, .fcpxmld 번들)
- 지원하지 않는 것: 속도 변경(timeMap), 오디션, 중첩 시퀀스 내부 (이름만 표시)

터미널에서 확인:  python3 fcpxml.py 타임라인.fcpxml
"""
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from fractions import Fraction

# 타임라인에 놓이는 요소들
CLIP_TAGS = {"asset-clip", "clip", "video", "audio", "title", "caption",
             "ref-clip", "mc-clip", "sync-clip", "gap", "spine"}
# 안에 연결 클립(lane)을 품을 수 있는 요소
HOLDER_TAGS = {"asset-clip", "clip", "video", "audio", "ref-clip", "mc-clip",
               "sync-clip", "gap", "title", "caption"}
TIMELINE_EXTS = (".fcpxml", ".fcpxmld")


def nfc(s):
    """맥 파일명은 한글이 분해된 형태(NFD)로 저장될 수 있어서 비교 전에 통일한다."""
    return unicodedata.normalize("NFC", s or "")


def parse_time(s):
    """'1001/30000s' · '10s' · '3600s' → Fraction(초)"""
    if s is None:
        return Fraction(0)
    s = s.strip()
    if s.endswith("s"):
        s = s[:-1]
    if not s:
        return Fraction(0)
    if "/" in s:
        n, d = s.split("/", 1)
        return Fraction(int(n), int(d))
    return Fraction(s)


def _f(x):
    return round(float(x), 4)


def _basename_from_src(src):
    if not src:
        return None
    path = urllib.parse.unquote(urllib.parse.urlparse(src).path or src)
    name = os.path.basename(path.rstrip("/"))
    return nfc(name) or None


def _read_xml_root(path):
    if os.path.isdir(path):  # .fcpxmld 번들
        inner = os.path.join(path, "Info.fcpxml")
        if not os.path.exists(inner):
            raise ValueError("fcpxmld 번들 안에 Info.fcpxml이 없습니다")
        path = inner
    root = ET.parse(path).getroot()
    if root.tag != "fcpxml":
        raise ValueError("FCPXML 파일이 아닙니다 (다빈치에서 'FCPXML'로 내보냈는지 확인해 주세요)")
    return root


class _Parser:
    def __init__(self, root):
        self.root = root
        res = root.find("resources")
        self.formats = {}
        self.assets = {}
        self.media = {}
        self.effects = {}
        if res is not None:
            for f in res.findall("format"):
                self.formats[f.get("id")] = f
            for a in res.findall("asset"):
                src = None
                mr = a.find("media-rep")
                if mr is not None:
                    src = mr.get("src")
                if not src:
                    src = a.get("src")
                self.assets[a.get("id")] = {
                    "name": a.get("name"),
                    "file": _basename_from_src(src),
                    "has_video": a.get("hasVideo") != "0",
                }
            for m in res.findall("media"):
                self.media[m.get("id")] = m.get("name")
            for e in res.findall("effect"):
                self.effects[e.get("id")] = e.get("name")
        self.clips = []

    # --- 요소 하나를 클립 정보로 변환 ---
    def _describe(self, el):
        tag = el.tag
        name = el.get("name")
        ref = el.get("ref")
        info = {"kind": "video", "name": name, "file": None}
        if tag in ("title", "caption"):
            info["kind"] = "title"
            info["name"] = name or self.effects.get(ref)
            text_el = el.findall(".//text")
            parts = []
            for t in text_el:
                parts.append("".join(t.itertext()))
            info["text"] = " ".join(" ".join(parts).split())
            return info
        if tag == "gap":
            info["kind"] = "gap"
            return info
        asset = self.assets.get(ref)
        if asset is None and tag == "clip":  # <clip> 안쪽의 video/audio/asset-clip에서 찾기
            for ch in el:
                if ch.tag in ("video", "audio", "asset-clip") and ch.get("ref") in self.assets:
                    asset = self.assets[ch.get("ref")]
                    break
        if asset is not None:
            info["file"] = asset["file"]
            info["name"] = name or asset["name"] or asset["file"]
            if not asset["has_video"] or tag == "audio":
                info["kind"] = "audio"
        else:
            if tag == "audio":
                info["kind"] = "audio"
            info["name"] = name or self.media.get(ref) or self.effects.get(ref)
        return info

    def walk(self, parent, abs_origin, local_origin, base_lane):
        """parent의 자식들을 훑는다.
        abs_origin: parent 내부 시간 local_origin 이 타임라인(0초 기준)에서 놓인 위치."""
        accepts_plain = parent.tag in ("spine", "sequence")
        for el in parent:
            tag = el.tag
            if tag not in CLIP_TAGS:
                continue
            if not accepts_plain and "lane" not in el.attrib and tag not in ("title", "caption", "spine"):
                continue  # <clip> 안의 video/audio 는 클립 본체이므로 제외
            offset = parse_time(el.get("offset")) if el.get("offset") is not None else local_origin
            start_abs = abs_origin + (offset - local_origin)
            lane = base_lane + int(el.get("lane", 0))

            if tag == "spine":  # 보조 스토리라인
                self.walk(el, start_abs, Fraction(0), lane)
                continue

            dur = parse_time(el.get("duration"))
            info = self._describe(el)
            info.update({
                "lane": lane,
                "s": start_abs,
                "e": start_abs + dur,
                "local_start": parse_time(el.get("start")),
                "tag": tag,
            })
            self.clips.append(info)
            if tag in HOLDER_TAGS:
                self.walk(el, start_abs, parse_time(el.get("start")), lane)


def _segments(vclips):
    """겹친 영상 클립 중 가장 위 레인 것만 남겨, 화면에 실제로 보이는 구간 목록을 만든다."""
    pts = sorted({c["s"] for c in vclips} | {c["e"] for c in vclips})
    segs = []
    for a, b in zip(pts, pts[1:]):
        cover = [i for i, c in enumerate(vclips) if c["s"] <= a and c["e"] >= b]
        top = max(cover, key=lambda i: (vclips[i]["lane"], i)) if cover else None
        if segs and segs[-1]["_i"] == top and segs[-1]["_e"] == a:
            segs[-1]["_e"] = b
            continue
        segs.append({"_i": top, "_s": a, "_e": b})
    out = []
    for sg in segs:
        i = sg["_i"]
        if i is None:
            out.append({"start": _f(sg["_s"]), "end": _f(sg["_e"]), "name": None, "file": None,
                        "lane": 0, "broll": False, "src_in": None})
            continue
        c = vclips[i]
        out.append({
            "start": _f(sg["_s"]), "end": _f(sg["_e"]),
            "name": c["name"], "file": c["file"], "lane": c["lane"], "broll": c["lane"] > 0,
            "src_in": _f(c["local_start"] + (sg["_s"] - c["s"])),
            "clip_start": _f(c["s"]), "clip_end": _f(c["e"]),
        })
    return out


def parse_timeline(path):
    root = _read_xml_root(path)
    p = _Parser(root)
    seq = root.find(".//sequence")
    if seq is None:
        raise ValueError("타임라인(sequence)을 찾을 수 없습니다")
    fmt = p.formats.get(seq.get("format"))
    fd = parse_time(fmt.get("frameDuration")) if fmt is not None and fmt.get("frameDuration") else Fraction(1, 30)
    tc_start = parse_time(seq.get("tcStart"))
    spine = seq.find("spine")
    if spine is None:
        raise ValueError("타임라인에 spine이 없습니다")
    p.walk(spine, Fraction(0), tc_start, 0)

    vclips = [c for c in p.clips if c["kind"] == "video"]
    subs = sorted(
        ({"start": _f(c["s"]), "end": _f(c["e"]), "text": c["text"], "lane": c["lane"]}
         for c in p.clips if c["kind"] == "title" and c.get("text")),
        key=lambda x: x["start"])
    project = root.find(".//project")
    return {
        "project": project.get("name") if project is not None else None,
        "fps": round(float(1 / fd), 3),
        "frame_duration": _f(fd),
        "duration": _f(parse_time(seq.get("duration"))),
        "tc_start": _f(tc_start),
        "frame_duration_frac": f"{fd.numerator}/{fd.denominator}",   # 마커 내보내기용 정확한 값
        "tc_start_frac": f"{tc_start.numerator}/{tc_start.denominator}",
        "tc_format": seq.get("tcFormat") or "NDF",
        "clips": [{"kind": c["kind"], "name": c["name"], "file": c["file"], "lane": c["lane"],
                   "start": _f(c["s"]), "end": _f(c["e"])} for c in p.clips if c["kind"] in ("video", "audio")],
        "segments": _segments(vclips),
        "subtitles": subs,
        "version": root.get("version"),
    }


# --- 영상 이름으로 타임라인 파일 찾기 ---
def list_timeline_files(folder):
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        if n.startswith("."):
            continue
        if n.lower().endswith(TIMELINE_EXTS):
            out.append(n)
    return sorted(out)


def _stem(name):
    return nfc(os.path.splitext(name)[0]).strip().lower()


def _base(stem):
    return re.sub(r"[\s_\-]*v\d+$", "", stem)


def _version(stem):
    m = re.search(r"v(\d+)$", stem)
    return int(m.group(1)) if m else -1


def find_timeline(folder, video_name):
    """같은 이름 → 같은 이름(버전 번호만 다름) 순으로 찾는다. 없으면 None."""
    files = list_timeline_files(folder)
    vs = _stem(video_name)
    for f in files:
        if _stem(f) == vs:
            return f
    same_base = [f for f in files if _base(_stem(f)) == _base(vs) and _base(vs)]
    if same_base:
        same_base.sort(key=lambda f: (_version(_stem(f)), os.path.getmtime(os.path.join(folder, f))))
        # 영상 버전과 가장 가까운(같거나 낮은) 것 우선, 없으면 가장 최신
        lower = [f for f in same_base if _version(_stem(f)) <= _version(vs)]
        return (lower or same_base)[-1]
    return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    data = parse_timeline(sys.argv[1])
    print(json.dumps({k: v for k, v in data.items() if k not in ("clips",)}, ensure_ascii=False, indent=1))
    print(f"\n영상/오디오 클립 {len(data['clips'])}개, 보이는 구간 {len(data['segments'])}개, 자막 {len(data['subtitles'])}개, {data['fps']}fps")
