#!/usr/bin/env python3
"""자체 테스트 — 진짜 파일을 건드리지 않고 '피드백 → 반영 → 검증 → JSON 체크' 전체 루프를 돌려 봐요.

사용:  python3 작업/feedback-tool/selftest.py ["타임라인/<파일>.fcpxml"] [--keep]
  - FCPXML을 주지 않으면 타임라인/ 폴더의 첫 FCPXML을 쓰고, 그것도 없으면 내장 예제를 써요.
  - 임시 폴더에 복사본을 만들어 테스트하므로 원본·컨펌본·소스는 전혀 바뀌지 않아요.
  - 테스트 피드백 3개: 컷 삭제 1, 길이 조절 1, 애매한 요청 1
  - --keep: 테스트 폴더를 지우지 않고 남김 (결과 FCPXML을 리졸브로 가져와 볼 수 있음)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import fcpxml_edit as fe  # noqa: E402

PASS, FAIL = "✓", "✗"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


def sample_fcpxml(path):
    """내장 예제: 29.97fps, 01:00:00:00 시작, 클립 3개 + 빈 구간 + B롤·자막·배경음악"""
    fd = Fraction(1001, 30000)

    def t(n):
        x = Fraction(n) * fd
        return f"{x.numerator}/{x.denominator}s"
    tc0 = 108000
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
    <resources>
        <format id="r0" frameDuration="1001/30000s" width="1920" height="1080"/>
        <asset id="r1" name="A" start="0s" duration="{t(3000)}" hasVideo="1" format="r0" hasAudio="1"><media-rep kind="original-media" src="file:///tmp/A.mov"/></asset>
        <asset id="r2" name="B" start="0s" duration="{t(600)}" hasVideo="1" format="r0" hasAudio="1"><media-rep kind="original-media" src="file:///tmp/B.mov"/></asset>
        <asset id="r3" name="C" start="0s" duration="{t(400)}" hasVideo="1" format="r0" hasAudio="1"><media-rep kind="original-media" src="file:///tmp/C.mov"/></asset>
        <asset id="r4" name="bgm" start="0s" duration="{t(5000)}" hasVideo="0" hasAudio="1"><media-rep kind="original-media" src="file:///tmp/bgm.wav"/></asset>
        <effect id="r5" name="Text+"/>
    </resources>
    <library><event name="selftest"><project name="selftest_v1">
        <sequence format="r0" duration="{t(1050)}" tcStart="{t(tc0)}" tcFormat="NDF">
            <spine>
                <asset-clip ref="r1" offset="{t(tc0)}" name="A" start="{t(300)}" duration="{t(450)}" format="r0">
                    <asset-clip ref="r2" lane="1" offset="{t(400)}" name="B" start="{t(50)}" duration="{t(120)}" format="r0"/>
                    <title ref="r5" lane="2" offset="{t(330)}" name="자막1" start="0s" duration="{t(60)}"><text><text-style ref="ts1">안녕하세요</text-style></text><text-style-def id="ts1"><text-style font="Helvetica"/></text-style-def></title>
                    <asset-clip ref="r4" lane="-1" offset="{t(300)}" name="bgm" start="0s" duration="{t(1050)}"/>
                </asset-clip>
                <gap offset="{t(tc0 + 450)}" name="Gap" start="{t(tc0)}" duration="{t(30)}"/>
                <asset-clip ref="r1" offset="{t(tc0 + 480)}" name="A" start="{t(1500)}" duration="{t(300)}" format="r0"/>
                <asset-clip ref="r3" offset="{t(tc0 + 780)}" name="C" start="0s" duration="{t(270)}" format="r0"/>
            </spine>
        </sequence>
    </project></event></library>
</fcpxml>
'''
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)


def content(path):
    """프레임마다 보이는 (레인, 소스/자막, 소스 안 위치) — 수정 전후를 프레임 단위로 비교하는 데 씀"""
    tl = fe.Timeline(path)
    n = tl.total_frames()
    m = [set() for _ in range(n)]
    for w in tl.walk():
        el = w["el"]
        if el.tag == "gap":
            continue
        ident = tl.text_of(el) if el.tag in ("title", "caption") else (tl.file_of(el) or el.get("name"))
        a, b = tl.frame_of(w["a"]), tl.frame_of(w["b"])
        for k in range(max(a, 0), min(b, n)):
            m[k].add((w["lane"], ident, fe.start(el) + (tl.abs_of(k) - w["a"])))
    return m


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep = "--keep" in sys.argv
    base = os.environ.get("FEEDBACK_BASE") or os.path.dirname(os.path.dirname(HERE))
    src = args[0] if args else None
    if not src:
        tdir = os.path.join(base, "타임라인")
        found = sorted(f for f in os.listdir(tdir) if f.lower().endswith(".fcpxml")) if os.path.isdir(tdir) else []
        src = os.path.join(tdir, found[0]) if found else None
    tmp = tempfile.mkdtemp(prefix="feedback-selftest-")
    for d in ("컨펌본", "타임라인", "소스", "작업/feedback"):
        os.makedirs(os.path.join(tmp, d), exist_ok=True)
    fx = os.path.join(tmp, "타임라인", "selftest_v1.fcpxml")
    if src:
        print(f"테스트 대상: {src} (복사본으로 테스트)")
        shutil.copy(src, fx)
    else:
        print("타임라인/에 FCPXML이 없어서 내장 예제로 테스트해요")
        sample_fcpxml(fx)
    open(os.path.join(tmp, "컨펌본", "selftest_v1.mp4"), "wb").close()  # 이름만 맞춘 빈 영상
    env = dict(os.environ, FEEDBACK_BASE=tmp)

    def run(*cmd):
        return subprocess.run([sys.executable, *cmd], cwd=tmp, env=env, capture_output=True, text=True)

    # 1) FCPXML 파악
    print("\n[1] FCPXML 파악")
    tl = fe.Timeline(fx)
    prim = [el for el in tl.primary() if el.tag != "gap"]
    check("읽기", True, f"FCPXML {tl.version} · {float(1 / tl.fd):.3f}fps · 시작 {tl.tc(0, True)} · 길이 {tl.tc(tl.total_frames())} · 클립 {len(prim)}개")
    if len(prim) < 2:
        check("테스트할 클립이 2개 이상", False, "클립이 너무 적어요")
        return finish(tmp, keep)
    longest = max(prim, key=fe.dur)
    lf0, lf1 = tl.frame_of(fe.off(longest)), tl.frame_of(fe.off(longest) + fe.dur(longest))
    cut_len = max(1, min(15, (lf1 - lf0) // 4))
    cut_a = (lf0 + lf1) // 2
    last = [el for el in prim if el is not longest][-1]
    la0, la1 = tl.frame_of(fe.off(last)), tl.frame_of(fe.off(last) + fe.dur(last))
    trim = max(1, min(10, (la1 - la0) // 4))

    # 2) 테스트 피드백 3개 (검토 페이지와 같은 저장 함수 사용)
    print("\n[2] 테스트 피드백 3개 남기기")
    import server
    d = server.Dirs(tmp)
    a1 = server.add_item(d, "selftest_v1.mp4", {"start_frame": cut_a, "end_frame": cut_a + cut_len, "type": "컷 삭제", "memo": "여기 말 더듬는 부분 잘라줘"})
    a2 = server.add_item(d, "selftest_v1.mp4", {"start_frame": la0 + 5, "type": "길이 조절", "memo": f"이 클립 끝을 {trim}프레임 줄여줘"})
    a3 = server.add_item(d, "selftest_v1.mp4", {"start_frame": (la0 + la1) // 2, "type": "기타", "memo": "여기 느낌이 좀 별로인데"})
    fbp = server.feedback_path(d, "selftest_v1.mp4")
    data = json.load(open(fbp, encoding="utf-8"))
    check("<영상>.feedback.json 생성", os.path.basename(fbp) == "selftest_v1.feedback.json", os.path.basename(fbp))
    check("id f001~f003 · 상태 todo · 타임코드 HH:MM:SS:FF",
          [i["id"] for i in data["items"]] == ["f001", "f002", "f003"] and all(i["status"] == "todo" for i in data["items"]),
          f"{a1['start_tc']}~{a1['end_tc']} / {a2['start_tc']} / {a3['start_tc']} (타임라인 {a1['timeline_start_tc']})")
    check("헤더: fps·FCPXML·타임라인 시작", data.get("fcpxml") == "타임라인/selftest_v1.fcpxml",
          f"fps {data.get('fps')} · tc_start {data.get('timeline_tc_start')}")

    # 3) Claude Code가 하는 것처럼: 지시서 → 계획 → 적용
    print("\n[3] 반영 (fb.py brief → fcpxml_edit.py apply)")
    r = run(os.path.join(HERE, "fb.py"), "brief", "selftest")
    check("작업 지시서", r.returncode == 0 and "f001" in r.stdout and "f003" in r.stdout, r.stderr.strip()[:100])
    plan = {"ops": [{"id": "f001", "op": "cut", "start": f"{a1['start_frame']}f", "end": f"{a1['end_frame']}f"},
                    {"id": "f002", "op": "length", "at": f"{a2['start_frame']}f", "delta_frames": -trim, "edge": "end"}]}
    pf = os.path.join(tmp, "plan.json")
    json.dump(plan, open(pf, "w", encoding="utf-8"))
    r = run(os.path.join(HERE, "fcpxml_edit.py"), "apply", "타임라인/selftest_v1.fcpxml", pf)
    if r.returncode != 0:
        check("적용", False, r.stderr.strip()[:200])
        return finish(tmp, keep)
    rep = json.loads(r.stdout)
    out = rep["out"]
    check("새 버전 저장 (원본 그대로)", out and out.endswith("selftest_v2.fcpxml") and os.path.exists(fx), os.path.basename(out or ""))
    for x in rep["results"]:
        check(f"{x['id']} {x['op']}", x["ok"], x["message"])

    # 4) 검증
    print("\n[4] 검증")
    for c in rep["validation"]["checks"]:
        check(c["name"], c["ok"], c["detail"])
    old, new = content(fx), content(os.path.join(tmp, out))
    removed = [(a1["start_frame"], a1["end_frame"]), (la1 - trim, la1)]
    exp = [old[k] for k in range(len(old)) if not any(a <= k < b for a, b in removed)]
    diff = sum(1 for k in range(max(len(new), len(exp))) if (new[k] if k < len(new) else None) != (exp[k] if k < len(exp) else None))
    check("프레임 단위 비교: 잘라낸 프레임만 빠지고 나머지는 그대로", diff == 0,
          f"원본 {len(old)} → 새 {len(new)}프레임 (예상 {len(exp)}), 다른 프레임 {diff}개")

    # 5) JSON 업데이트
    print("\n[5] JSON 업데이트 (fb.py done / question)")
    newname = os.path.basename(out)
    r1 = run(os.path.join(HERE, "fb.py"), "done", "f001", f"{a1['start_tc']}~{a1['end_tc']} {cut_len}프레임 삭제, 뒤 클립 당김", "--in", newname)
    r2 = run(os.path.join(HERE, "fb.py"), "done", "f002", f"마지막 클립 끝 {trim}프레임 줄임", "--in", newname)
    r3 = run(os.path.join(HERE, "fb.py"), "question", "f003", "어떤 점이 별로인지(색감·속도·음악) 알려 주세요")
    check("상태 기록 명령", all(x.returncode == 0 for x in (r1, r2, r3)), (r1.stderr + r2.stderr + r3.stderr).strip()[:150])
    data = json.load(open(fbp, encoding="utf-8"))
    st = {i["id"]: (i["status"], i.get("resolved_in"), bool(i.get("claude_note"))) for i in data["items"]}
    check("f001·f002 done + resolved_in + claude_note", st["f001"] == ("done", newname, True) and st["f002"] == ("done", newname, True))
    check("f003 question (애매한 요청은 추측하지 않음)", st["f003"][0] == "question")
    r = run(os.path.join(HERE, "fb.py"), "brief", "selftest")
    check("대기 피드백이 남지 않음", "대기 중인 피드백이 없어요" in r.stdout)
    finish(tmp, keep, os.path.join(tmp, out))


def finish(tmp, keep, out=None):
    ok = all(results)
    print(f"\n결과: {sum(results)}/{len(results)} 통과 — " + ("모두 통과 ✓" if ok else "실패한 항목이 있어요 ✗"))
    if keep:
        print(f"테스트 폴더: {tmp}" + (f"\n결과 FCPXML: {out}" if out else ""))
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
