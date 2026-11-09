"""구글 드라이브 영상 링크 + 고객사 원격 피드백 (Python 표준 라이브러리만 사용)

- 구글 드라이브에 올린 영상을 링크로 등록하면 검토 페이지에서 바로 재생돼요.
- 고객사용 페이지는 구글(Apps Script)에 올라가 있어요. 내 맥은 그 페이지와 피드백을 주고받기만 해요.
  (내 맥의 검토 페이지는 계속 내 맥에서만 열려요 — Claude Code 버튼이 바깥에 열리지 않게)

설정 파일: 작업/feedback/links.json
  {"remote": {"url": "<Apps Script 주소>", "key": "<연결 비밀키>"},
   "videos": {"<이름>": {"drive_id": "...", "fps": "30000/1001", "token": "<고객 링크 키>", ...}}}
"""
import json
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

LINK_EXT = ".gdrive"  # 링크로 등록한 영상은 목록에서 "<이름>.gdrive" 로 다뤄요 (실제 파일은 없음)
_lock = threading.RLock()
_last_sync = {}  # 영상 이름 → 마지막 동기화 시각
_sync_running = set()


class RemoteError(Exception):
    pass


# ---------- 구글 드라이브 링크 ----------
def drive_id_of(url):
    """구글 드라이브 공유 링크에서 파일 ID 꺼내기"""
    url = (url or "").strip()
    for pat in (r"/file/d/([A-Za-z0-9_-]{10,})", r"[?&]id=([A-Za-z0-9_-]{10,})", r"^([A-Za-z0-9_-]{25,})$"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def media_url(drive_id):
    """드라이브 파일 내려받기 주소 (confirm=t: 큰 파일의 '바이러스 검사' 안내를 건너뜀).
    구글이 다른 사이트의 <video>에서 직접 여는 걸 막아서, 내 맥 서버가 대신 받아서 검토 페이지에 넘겨줘요."""
    return f"https://drive.usercontent.google.com/download?id={drive_id}&export=download&confirm=t"


def api_media_url(drive_id, key):
    """드라이브 API 주소 (API 키 필요) — 고객 페이지가 브라우저에서 바로 재생할 때 씀"""
    return f"https://www.googleapis.com/drive/v3/files/{drive_id}?alt=media&key={key}"


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def open_drive(d, drive_id, rng=None):
    """드라이브 영상을 내 맥 서버가 받아 옴 (Range 그대로 전달) → (응답, 상태코드)"""
    key = remote_conf(d).get("drive_key")
    urls = ([api_media_url(drive_id, key)] if key else []) + [media_url(drive_id)]
    if os.environ.get("FEEDBACK_DRIVE_TEST_URL"):  # (자체 테스트 전용)
        urls = [os.environ["FEEDBACK_DRIVE_TEST_URL"].format(id=drive_id)]
    last = None
    for u in urls:
        headers = {"User-Agent": UA}
        if rng:
            headers["Range"] = rng
        try:
            r = urllib.request.urlopen(urllib.request.Request(u, headers=headers), timeout=30)
        except urllib.error.HTTPError as e:
            last = f"구글 드라이브가 거절했어요 ({e.code}) — 공유가 '링크가 있는 모든 사용자'인지 확인해 주세요"
            continue
        except (urllib.error.URLError, OSError) as e:
            last = f"구글 드라이브에 연결하지 못했어요 (인터넷 연결 확인): {getattr(e, 'reason', e)}"
            continue
        ctype = r.headers.get("Content-Type", "")
        if ctype.startswith("text/html"):  # 로그인·권한 안내 화면이 온 경우
            r.close()
            last = "구글 드라이브가 영상 대신 안내 화면을 보냈어요 — 공유가 '링크가 있는 모든 사용자'인지 확인해 주세요"
            continue
        return r, r.status
    raise RemoteError(last or "영상을 받아 오지 못했어요")


def check_drive_key(key, drive_id=None):
    """드라이브 API 키가 맞는지 확인 (영상 정보 읽어 보기)"""
    key = (key or "").strip()
    if not re.match(r"^[A-Za-z0-9_-]{30,60}$", key):
        raise ValueError("API 키 모양이 아니에요 (보통 AIza로 시작하는 39글자)")
    if not drive_id or os.environ.get("FEEDBACK_DRIVE_TEST_URL"):
        return {"ok": True}
    u = f"https://www.googleapis.com/drive/v3/files/{drive_id}?fields=name,size,videoMediaMetadata&key={key}"
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=20) as r:
            return {"ok": True, "file": json.loads(r.read().decode("utf-8"))}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8"))["error"]["message"]
        except Exception:
            msg = str(e.code)
        if "not been used" in msg or "disabled" in msg:
            msg = "이 프로젝트에서 Google Drive API를 '사용'으로 켜 주세요"
        elif "API key not valid" in msg:
            msg = "API 키가 올바르지 않아요"
        elif e.code == 404:
            msg = "키는 맞는데 영상을 찾을 수 없어요 — 공유가 '링크가 있는 모든 사용자'인지 확인해 주세요"
        raise RemoteError("API 키 확인 실패: " + msg)
    except (urllib.error.URLError, OSError) as e:
        raise RemoteError(f"구글에 연결하지 못했어요: {getattr(e, 'reason', e)}")


def view_url(drive_id):
    return f"https://drive.google.com/file/d/{drive_id}/view"


# ---------- links.json ----------
def links_path(d):
    return os.path.join(d.feedback, "links.json")


def load_links(d):
    try:
        with open(links_path(d), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("remote", {})
    data.setdefault("videos", {})
    return data


def save_links(d, data):
    os.makedirs(d.feedback, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d.feedback, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, links_path(d))


def is_link(video_actual):
    return str(video_actual).endswith(LINK_EXT)


def link_name(video_actual):
    return str(video_actual)[: -len(LINK_EXT)] if is_link(video_actual) else None


def link_info(d, video_actual):
    name = link_name(video_actual)
    return load_links(d)["videos"].get(name) if name else None


def clean_name(name):
    """영상 이름: 파일 이름으로 쓸 수 있게 정리 (타임라인 FCPXML과 같은 이름이면 자동으로 짝지어져요)"""
    name = re.sub(r"\.(mp4|mov|m4v|webm|gdrive)$", "", (name or "").strip(), flags=re.I)
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", name).strip(" .")
    return name[:80]


FPS_OK = {"24000/1001", "24/1", "25/1", "30000/1001", "30/1", "50/1", "60000/1001", "60/1"}


def add_link(d, url, name, fps=None):
    did = drive_id_of(url)
    if not did:
        raise ValueError("구글 드라이브 공유 링크가 아니에요 (예: https://drive.google.com/file/d/…/view)")
    name = clean_name(name)
    if not name:
        raise ValueError("영상 이름을 적어 주세요")
    if fps and fps not in FPS_OK:
        raise ValueError("프레임레이트 값이 올바르지 않아요")
    with _lock:
        data = load_links(d)
        old = data["videos"].get(name, {})
        data["videos"][name] = {**old, "drive_id": did, "fps": fps or old.get("fps"),
                                "added_at": old.get("added_at") or datetime.now().astimezone().isoformat(timespec="seconds")}
        if old.get("drive_id") and old.get("drive_id") != did:
            data["videos"][name].pop("token", None)  # 다른 영상으로 바꿨으면 고객 링크도 새로
        save_links(d, data)
    return name + LINK_EXT


def remove_link(d, video_actual):
    name = link_name(video_actual)
    with _lock:
        data = load_links(d)
        if data["videos"].pop(name, None) is None:
            raise KeyError("등록된 링크가 아니에요")
        save_links(d, data)


def list_links(d):
    """목록에 끼워 넣을 링크 영상들"""
    out = []
    for name, v in load_links(d)["videos"].items():
        if not v.get("drive_id"):
            continue
        try:
            mt = datetime.fromisoformat(v.get("added_at")).timestamp()
        except (TypeError, ValueError):
            mt = 0
        out.append({"actual": name + LINK_EXT, "name": name + LINK_EXT, "size": 0, "mtime": mt,
                    "url": "/media/" + urllib.parse.quote(name + LINK_EXT), "drive_id": v["drive_id"]})
    return out


# ---------- 구글(Apps Script)과 주고받기 ----------
def remote_conf(d):
    return load_links(d)["remote"]


def client_url(d, token):
    """고객에게 보낼 주소. GitHub Pages 주소가 있으면 그 페이지(+ 구글 저장소 ID), 없으면 Apps Script 페이지"""
    conf = remote_conf(d)
    base = conf.get("url")
    if not base or not token:
        return None
    pages = conf.get("pages_url")
    if pages:
        m = re.match(r"^https://script\.google\.com/macros/s/([A-Za-z0-9_-]+)/exec$", base)
        sid = m.group(1) if m else urllib.parse.quote(base, safe="")  # (테스트 주소는 통째로)
        return f"{pages.rstrip('/')}/?p={token}&s={sid}"
    return f"{base}?p={token}"


def set_pages(d, url):
    """고객 페이지를 GitHub Pages 등 다른 곳에 올렸을 때 그 주소 (빈 값이면 Apps Script 페이지 사용)"""
    url = (url or "").strip()
    if url and not re.match(r"^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~/-]*)?$", url) and not os.environ.get("FEEDBACK_REMOTE_TEST"):
        raise ValueError("https:// 로 시작하는 주소를 적어 주세요 (예: https://아이디.github.io/video-feedback/)")
    with _lock:
        data = load_links(d)
        if url:
            data["remote"]["pages_url"] = url
        else:
            data["remote"].pop("pages_url", None)
        save_links(d, data)
    return {"ok": True}


def call(d, action, payload=None, timeout=25, conf=None):
    """Apps Script 웹 앱에 요청 보내기 (POST → 구글이 결과 주소로 넘겨주면 그 주소에서 받아 옴)"""
    conf = conf or remote_conf(d)
    if not conf.get("url") or not conf.get("key"):
        raise RemoteError("고객 피드백 연결이 아직 설정되지 않았어요")
    body = json.dumps({"action": action, "key": conf["key"], **(payload or {})}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(conf["url"], data=body, method="POST",
                                 headers={"Content-Type": "text/plain;charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RemoteError(f"구글 서버 응답 오류 ({e.code}) — 배포 주소와 '액세스: 모든 사용자' 설정을 확인해 주세요")
    except (urllib.error.URLError, OSError) as e:
        raise RemoteError(f"구글에 연결하지 못했어요 (인터넷 연결 확인): {getattr(e, 'reason', e)}")
    try:
        res = json.loads(raw)
    except ValueError:
        hint = "로그인" if "accounts.google.com" in raw or "Sign in" in raw else "응답"
        raise RemoteError(f"구글 페이지가 JSON 대신 {hint} 화면을 돌려줬어요 — 배포할 때 '액세스 권한: 모든 사용자'로 했는지 확인해 주세요")
    if not res.get("ok"):
        raise RemoteError(res.get("error") or "알 수 없는 오류")
    return res


def set_drive_key(d, key, drive_id=None):
    """고객 페이지 재생용 드라이브 API 키 저장 (확인 후 구글 쪽에도 알려 줌)"""
    key = (key or "").strip()
    info = check_drive_key(key, drive_id)
    with _lock:
        data = load_links(d)
        data["remote"]["drive_key"] = key
        save_links(d, data)
    if data["remote"].get("url") and data["remote"].get("key"):
        call(d, "settings", {"drive_key": key})
    return info


def setup(d, url):
    """Apps Script 주소를 등록하고 연결 확인 (처음 연결한 맥의 비밀키를 구글 쪽이 기억해요)"""
    url = (url or "").strip()
    ok = re.match(r"^https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec$", url)
    if not ok and not os.environ.get("FEEDBACK_REMOTE_TEST"):  # (자체 테스트 때만 다른 주소 허용)
        raise ValueError("Apps Script 웹 앱 주소가 아니에요 (https://script.google.com/macros/s/…/exec 형태)")
    with _lock:
        data = load_links(d)
        key = data["remote"].get("key") if data["remote"].get("url") == url else None
        conf = {**data["remote"], "url": url, "key": key or secrets.token_urlsafe(24)}
        res = call(d, "hello", conf=conf)
        data["remote"] = conf
        save_links(d, data)
    if conf.get("drive_key"):
        call(d, "settings", {"drive_key": conf["drive_key"]})
    return {"ok": True, "sheet_url": res.get("sheet_url"), "version": res.get("version")}


def share(d, video_actual, fps_str, title=None):
    """고객 링크 만들기 (이미 있으면 그대로 돌려줌)"""
    name = link_name(video_actual)
    if not name:
        raise ValueError("고객 링크는 구글 드라이브 링크로 등록한 영상만 만들 수 있어요 (고객이 영상을 볼 수 있어야 해서요)")
    with _lock:
        data = load_links(d)
        v = data["videos"].get(name)
        if not v:
            raise KeyError("등록된 링크가 아니에요")
        res = call(d, "project", {"token": v.get("token"), "name": title or name, "drive_id": v["drive_id"], "fps": fps_str})
        v["token"] = res["token"]
        save_links(d, data)
    return {"token": v["token"], "client_url": client_url(d, v["token"])}


def sync(d, video_actual, fb_api, force=True):
    """고객 피드백 가져오기 + 내 쪽 처리 상태(반영·질문·보류) 알려 주기.
    fb_api: server 모듈 (load_feedback/save_feedback/add_item/delete_item 등을 씀)"""
    name = link_name(video_actual)
    info = link_info(d, video_actual)
    if not info or not info.get("token"):
        return {"skipped": True}
    if not force and time.time() - _last_sync.get(name, 0) < 20:
        return {"skipped": True}
    _last_sync[name] = time.time()
    fb = fb_api.load_feedback(d, video_actual)
    by_remote = {it["remote_id"]: it for it in fb["items"] if it.get("remote_id")}
    updates = [{"id": rid, "status": it.get("status", "todo")} for rid, it in by_remote.items()]
    res = call(d, "sync", {"token": info["token"], "updates": updates, "known": list(by_remote)})
    added = changed = removed = 0
    for row in res.get("items", []):
        rid = row.get("id")
        local = by_remote.get(rid)
        if row.get("deleted"):
            if local and local.get("status") == "todo":  # 고객이 지운 피드백 (아직 반영 전일 때만)
                fb_api.delete_item(d, video_actual, {"id": local["id"]})
                removed += 1
            continue
        if local is None:
            fb_api.add_item(d, video_actual, {
                "start_frame": int(row.get("start_frame") or 0),
                "end_frame": int(row["end_frame"]) if row.get("end_frame") not in (None, "") else None,
                "type": row.get("type") or "기타", "memo": row.get("memo") or "(내용 없음)",
                "snapshot": row.get("snapshot"),
                "_extra": {"remote_id": rid, "origin": "client", "author": (row.get("author") or "고객")[:40]}})
            added += 1
        elif local.get("status") == "todo" and row.get("memo") and row["memo"] != local.get("memo"):
            fb_api.update_item(d, video_actual, {"id": local["id"], "memo": row["memo"], "type": row.get("type")})
            changed += 1
    return {"added": added, "changed": changed, "removed": removed, "client_url": client_url(d, info["token"])}


def sync_background(d, video_actual, fb_api):
    """검토 페이지가 열려 있을 때 20초마다 조용히 동기화 (실패해도 페이지는 그대로)"""
    name = link_name(video_actual)
    info = link_info(d, video_actual)
    if not name or not info or not info.get("token") or name in _sync_running:
        return
    if time.time() - _last_sync.get(name, 0) < 20:
        return

    def run():
        _sync_running.add(name)
        try:
            sync(d, video_actual, fb_api, force=False)
        except Exception:
            pass
        finally:
            _sync_running.discard(name)
    threading.Thread(target=run, daemon=True).start()
