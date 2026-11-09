"""검토 페이지에서 Claude Code에게 피드백 반영을 맡기는 기능.

'반영 맡기기' 버튼 → claude -p (화면 없는 실행) 로 fb-apply 스킬을 돌리고,
진행 상황(stream-json)을 받아 검토 페이지에 보여준다. 표준 라이브러리만 사용.
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid

SYSTEM_NOTE = (
    "이 실행은 사용자가 영상 검토 페이지에서 'Claude Code에게 반영 맡기기' 버튼을 눌러 시작한 화면 없는 작업이다. "
    "사용자는 터미널이 아니라 검토 페이지에서 진행 상황과 마지막 보고만 본다. "
    "도중에 사용자에게 질문할 수 없으므로, 의미가 애매한 항목은 추측해서 고치지 말고 "
    "`python3 작업/feedback-tool/fb.py question <id> \"질문\"` 으로, 할 수 없는 항목은 `skip <id> \"이유\"` 로 남겨라. "
    "마지막 메시지는 한국어로 짧게: 반영 n개 / 질문 n개 / 보류 n개, 새 파일 경로, 질문 목록, 리졸브에서 확인할 점."
)
CANDIDATES = ["~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
EXTRA_PATH = ["~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
MAX_EVENTS = 600


def find_claude():
    p = shutil.which("claude")
    if p:
        return p
    for c in CANDIDATES:
        c = os.path.expanduser(c)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _short(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_tool(name, inp):
    """도구 호출을 사람이 읽기 쉬운 한 줄로"""
    inp = inp or {}
    if name == "Bash":
        c = inp.get("command", "")
        if "fcpxml_edit.py" in c:
            for key, label in ((" apply ", "🎬 FCPXML 새 버전 만드는 중"), (" validate", "🔎 새 FCPXML 검증 중"),
                               (" info", "📋 타임라인 구조 확인"), (" locate", "📍 피드백 위치의 클립 찾는 중")):
                if key in c + " ":
                    return label
        if "fb.py" in c:
            for key, label in ((" done ", "✓ 반영 완료로 기록"), (" skip ", "⏸ 보류로 기록"), (" question ", "❓ 질문으로 남김"),
                               (" publish ", "⬆ 새 버전을 컨펌본에 올리는 중"), (" brief", "📋 피드백 지시서 읽는 중"),
                               (" frame ", "🖼 새 버전 화면 확인 중"), (" status", "📋 피드백 현황 확인"),
                               (" list", "📋 피드백 목록 확인")):
                if key in c + " ":
                    return label
        return "▶ 실행: " + _short(inp.get("description") or c, 140)
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return "✏️ 수정: " + os.path.basename(inp.get("file_path", "") or inp.get("notebook_path", "") or "")
    if name == "Read":
        fp = inp.get("file_path", "") or ""
        return "👀 피드백 화면 캡처 확인" if "/snapshots/" in fp else "👀 읽기: " + os.path.basename(fp)
    if name in ("Grep", "Glob"):
        return "🔍 찾기: " + _short(inp.get("pattern", ""), 60)
    if name == "Skill":
        return "📘 스킬 사용: " + str(inp.get("skill") or inp.get("command") or inp.get("name") or "")
    if name in ("Task", "Agent"):
        return "🧩 하위 작업: " + _short(inp.get("description", ""), 80)
    if name in ("TodoWrite", "TaskCreate", "TaskUpdate"):
        return "📝 할 일 정리"
    return "🔧 " + name


def _notify(title, msg):
    """맥 알림 (다른 창을 보고 있어도 끝난 걸 알 수 있게)"""
    if not shutil.which("osascript"):
        return
    esc = lambda s: str(s).replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(msg)}" with title "{esc(title)}"'],
                       timeout=5, capture_output=True)
    except Exception:
        pass


class JobManager:
    def __init__(self, dirs, load_feedback, production_for, list_videos, nfc):
        self.d = dirs
        self.load_feedback = load_feedback
        self.production_for = production_for
        self.list_videos = list_videos
        self.nfc = nfc
        self.lock = threading.RLock()
        self.job = None
        self.jobs_dir = os.path.join(dirs.feedback, "jobs")
        self._version = None
        self._load_last()

    # ---------- 상태 ----------
    def _load_last(self):
        p = os.path.join(self.jobs_dir, "latest.json")
        try:
            with open(p, "r", encoding="utf-8") as f:
                job = json.load(f)
            if job.get("status") == "running":  # 서버가 꺼지면서 끊긴 작업
                job["status"] = "interrupted"
                job["error"] = "검토 서버가 꺼지면서 작업 표시가 끊겼어요. 피드백 상태를 확인해 주세요."
            self.job = job
        except (OSError, ValueError):
            self.job = None

    def _save(self, job):
        os.makedirs(self.jobs_dir, exist_ok=True)
        data = {k: v for k, v in job.items() if not k.startswith("_")}
        for name in (job["id"] + ".json", "latest.json"):
            tmp = os.path.join(self.jobs_dir, "." + name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(self.jobs_dir, name))

    def info(self):
        path = find_claude()
        if path and self._version is None:
            try:
                r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=8, env=self._env())
                self._version = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
            except Exception:
                self._version = ""
        return {"available": bool(path), "path": path, "version": self._version or ""}

    def snapshot(self, after=0):
        with self.lock:
            if not self.job:
                return {"job": None}
            data = {k: v for k, v in self.job.items() if not k.startswith("_") and k != "events"}
            ev = self.job.get("events", [])
            data["events"] = ev[after:]
            data["event_count"] = len(ev)
            if data.get("status") == "running":
                data["elapsed"] = round(time.time() - data["started"])
            return {"job": data}

    def _add(self, job, text, kind="step"):
        with self.lock:
            job["events"].append({"t": round(time.time() - job["started"], 1), "kind": kind, "text": text})
            if len(job["events"]) > MAX_EVENTS:
                job["events"] = job["events"][-MAX_EVENTS:]

    @staticmethod
    def _env():
        env = dict(os.environ)
        extra = [os.path.expanduser(p) for p in EXTRA_PATH]
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
        return env

    # ---------- 시작 / 중지 ----------
    def start(self, video_actual, cont=True):
        with self.lock:
            if self.job and self.job.get("status") == "running":
                raise ValueError("Claude Code가 이미 작업 중이에요. 끝나거나 중지한 뒤에 다시 눌러 주세요")
            claude = find_claude()
            if not claude:
                raise ValueError("Claude Code(claude)를 찾지 못했어요. '시작하기'로 먼저 설치해 주세요")
            fb = self.load_feedback(self.d, video_actual)
            pending = [i["id"] for i in fb.get("items", []) if i.get("status") in ("todo", "pending", None)]
            if not pending:
                raise ValueError("대기 중인 피드백이 없어요")
            job = {
                "id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4],
                "video": self.nfc(video_actual), "status": "running", "started": time.time(),
                "continue": bool(cont), "pending_ids": pending, "events": [], "result": "", "error": "",
                "counts": None, "new_videos": [], "cost_usd": None, "turns": None,
            }
            self.job = job
            self._save(job)
            threading.Thread(target=self._run, args=(job, claude, video_actual), daemon=True).start()
            return self.snapshot()["job"]

    def cancel(self):
        with self.lock:
            job = self.job
            if not job or job.get("status") != "running":
                raise ValueError("진행 중인 작업이 없어요")
            job["_cancel"] = True
            proc = job.get("_proc")
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            def _force():
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
            threading.Timer(6, _force).start()
        self._add(job, "■ 중지를 요청했어요", "warn")

    # ---------- 실행 ----------
    def _command(self, claude, video_actual, cont):
        stem = os.path.splitext(self.nfc(video_actual))[0]
        prompt = (f"/apply-feedback 절차(작업/CLAUDE.md)대로 '{stem}' 영상의 대기 중인 피드백을 반영해줘. "
                  f"지시서는 `python3 작업/feedback-tool/fb.py brief \"{stem}\"` 로 본다.")
        cmd = [claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--permission-mode", "auto", "--append-system-prompt", SYSTEM_NOTE]
        prod = self.production_for(self.d, video_actual) or {}
        pd = prod.get("project_dir")
        base = os.path.realpath(self.d.base)
        if pd and os.path.isdir(pd) and not os.path.realpath(pd).startswith(base + os.sep):
            cmd += ["--add-dir", pd]  # 영상 프로젝트 폴더도 고칠 수 있게
        if cont:
            cmd += ["--continue", "--fork-session"]  # 영상을 만들던 대화를 이어받되, 열려 있는 대화는 건드리지 않게 분기
        return cmd

    def _run(self, job, claude, video_actual):
        before = {v["name"] for v in self.list_videos(self.d)}
        os.makedirs(self.jobs_dir, exist_ok=True)
        log_path = os.path.join(self.jobs_dir, job["id"] + ".log")
        self._add(job, "🤖 Claude Code에게 맡겼어요" + (" (영상을 만들던 대화를 이어서)" if job["continue"] else ""))
        rc, got_output, stderr_tail = self._attempt(job, claude, video_actual, job["continue"], log_path)
        if (rc != 0 and not got_output and job["continue"] and not job.get("_cancel")):
            self._add(job, "ℹ 이어갈 이전 대화가 없어서 새 대화로 시작해요")
            job["continue"] = False
            rc, got_output, stderr_tail = self._attempt(job, claude, video_actual, False, log_path)
        # 마무리
        try:
            fb = self.load_feedback(self.d, video_actual)
            st = {i["id"]: i.get("status", "todo") for i in fb.get("items", [])}
            ids = job["pending_ids"]
            job["counts"] = {k: sum(st.get(i) == k for i in ids) for k in ("done", "question", "skipped")}
            job["counts"]["pending"] = sum(st.get(i, "todo") == "todo" for i in ids)
        except Exception:
            job["counts"] = None
        job["new_videos"] = sorted({v["name"] for v in self.list_videos(self.d)} - before)
        with self.lock:
            if job.get("_cancel"):
                job["status"] = "cancelled"
            elif rc == 0 and not job.get("_result_error"):
                job["status"] = "done"
            else:
                job["status"] = "failed"
                if not job["error"]:
                    job["error"] = stderr_tail or f"Claude Code가 오류로 끝났어요 (코드 {rc})"
            job["finished"] = time.time()
            job["duration"] = round(job["finished"] - job["started"])
        c = job["counts"] or {}
        label = {"done": "작업 끝", "failed": "작업이 멈췄어요", "cancelled": "작업을 중지했어요"}[job["status"]]
        self._add(job, f"{'✅' if job['status'] == 'done' else '⛔'} {label} · 반영 {c.get('done', 0)} · 질문 {c.get('question', 0)} · 보류 {c.get('skipped', 0)}"
                  + (f" · 새 버전 {', '.join(job['new_videos'])}" if job["new_videos"] else ""), "end")
        self._save(job)
        _notify("영상 피드백", f"{label} — 반영 {c.get('done', 0)} · 질문 {c.get('question', 0)} · 보류 {c.get('skipped', 0)}"
                + (f" · {job['new_videos'][-1]}" if job["new_videos"] else ""))

    def _attempt(self, job, claude, video_actual, cont, log_path):
        cmd = self._command(claude, video_actual, cont)
        try:
            proc = subprocess.Popen(cmd, cwd=self.d.base, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1, env=self._env(), start_new_session=True)
        except OSError as e:
            job["error"] = f"Claude Code를 실행하지 못했어요: {e}"
            return 1, False, ""
        job["_proc"] = proc
        err_lines = []

        def read_err():
            for line in proc.stderr:
                err_lines.append(line.rstrip())
                del err_lines[:-20]
        threading.Thread(target=read_err, daemon=True).start()
        got = False
        with open(log_path, "a", encoding="utf-8") as log:
            for line in proc.stdout:
                log.write(line)
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    self._add(job, _short(line, 200), "raw")
                    continue
                if o.get("type") in ("assistant", "result"):
                    got = True
                self._handle(job, o)
                if len(job["events"]) % 10 == 0:
                    self._save(job)
        rc = proc.wait()
        time.sleep(0.2)
        return rc, got, "\n".join(err_lines[-6:])

    def _handle(self, job, o):
        t = o.get("type")
        if t == "system" and o.get("subtype") == "init":
            job["session_id"] = o.get("session_id")
            self._add(job, "▶ Claude Code 시작" + (f" ({o.get('model')})" if o.get("model") else ""))
        elif t == "assistant":
            for b in (o.get("message") or {}).get("content", []) or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    self._add(job, "💬 " + _short(b["text"], 400), "text")
                elif b.get("type") == "tool_use":
                    self._add(job, describe_tool(b.get("name", ""), b.get("input")))
        elif t == "user":
            for b in (o.get("message") or {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    c = b.get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    self._add(job, "⚠ " + _short(c, 200), "warn")
        elif t == "result":
            job["result"] = o.get("result") or ""
            job["cost_usd"] = o.get("total_cost_usd")
            job["turns"] = o.get("num_turns")
            if o.get("is_error") or (o.get("subtype") not in (None, "success")):
                job["_result_error"] = True
                job["error"] = _short(o.get("result") or o.get("subtype") or "오류", 400)
