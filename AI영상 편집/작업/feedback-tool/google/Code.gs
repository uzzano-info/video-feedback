/**
 * 영상 피드백 — 고객사용 페이지 (구글 Apps Script)
 *
 * 하는 일
 *  - 고객이 링크(…/exec?p=<키>)로 들어오면 영상(구글 드라이브)을 보며 시간별 피드백을 남겨요.
 *  - 피드백은 내 구글 드라이브의 시트 "영상 피드백 (고객사)"에 저장돼요.
 *  - 내 맥의 피드백 도구가 이 주소로 피드백을 가져가고, 반영 상태를 알려 줘요.
 *
 * 설치: script.google.com ▸ 새 프로젝트 ▸ 이 코드를 통째로 붙여넣기 ▸ 저장
 *       ▸ 배포 ▸ 새 배포 ▸ 유형: 웹 앱 ▸ 다음 사용자로 실행: 나 ▸ 액세스 권한: 모든 사용자 ▸ 배포
 *       ▸ 나온 웹 앱 주소(…/exec)를 맥의 검토 페이지 "고객 피드백 연결"에 붙여넣기
 * 다른 맥으로 연결을 옮기려면: 위쪽 함수 선택에서 resetConnection 을 고르고 ▶ 실행
 */
var VERSION = '2026.11.09';
var BOOK_NAME = '영상 피드백 (고객사)';
var TYPES = ['컷 삭제', '길이 조절', '순서 변경', '자막·텍스트 수정', '클립 교체', '기타'];
var P_COLS = ['token', 'name', 'drive_id', 'fps', 'created_at'];
var F_COLS = ['id', 'token', 'start_frame', 'end_frame', 'type', 'memo', 'author', 'owner', 'created_at', 'updated_at', 'status', 'deleted', 'snapshot'];
var STATUSES = ['todo', 'done', 'question', 'skipped'];

// ---------- 시트 ----------
function props_() { return PropertiesService.getScriptProperties(); }

function book_() {
  var id = props_().getProperty('SHEET_ID'), ss = null;
  if (id) { try { ss = SpreadsheetApp.openById(id); } catch (e) { ss = null; } }
  if (!ss) {   // 처음 한 번: 내 드라이브에 시트를 만들어요
    ss = SpreadsheetApp.create(BOOK_NAME);
    props_().setProperty('SHEET_ID', ss.getId());
  }
  sheet_(ss, 'projects', P_COLS);
  sheet_(ss, 'feedback', F_COLS);
  return ss;
}

function sheet_(ss, name, cols) {
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.getRange(1, 1, 1, cols.length).setValues([cols]);
    sh.setFrozenRows(1);
    ss.getSheets().forEach(function (x) {   // 처음 생긴 빈 시트(시트1)는 지움
      if (x.getName() !== 'projects' && x.getName() !== 'feedback' && x.getLastRow() === 0 && ss.getSheets().length > 1) ss.deleteSheet(x);
    });
  }
  return sh;
}

function rows_(sh, cols) {
  var n = sh.getLastRow();
  if (n < 2) return [];
  var vals = sh.getRange(2, 1, n - 1, cols.length).getValues();
  return vals.map(function (r, i) {
    var o = { _row: i + 2 };
    cols.forEach(function (c, j) { o[c] = r[j]; });
    return o;
  });
}

function text_(s, max) { return String(s == null ? '' : s).slice(0, max || 2000); }

// 시트에 쓸 때 글자는 항상 "글자 그대로" 저장 (앞에 ' 를 붙이면 수식·날짜·숫자로 바뀌지 않아요. 예: "30/1", "=SUM")
function cell_(x) { return typeof x === 'string' && x !== '' ? "'" + x : x; }

function setCells_(sh, row, cols, obj) {
  Object.keys(obj).forEach(function (k) {
    var j = cols.indexOf(k);
    if (j >= 0) sh.getRange(row, j + 1).setValue(cell_(obj[k]));
  });
}

function withLock_(fn) {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try { return fn(); } finally { lock.releaseLock(); }
}

function project_(token) {
  if (!token) return null;
  var sh = book_().getSheetByName('projects');
  var found = rows_(sh, P_COLS).filter(function (p) { return String(p.token) === String(token); });
  return found[0] || null;
}

function now_() { return new Date().toISOString(); }
function newId_() { return 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7); }
function int_(v) { var n = parseInt(v, 10); return isNaN(n) || n < 0 ? null : n; }

function clientRow_(r, owner) {
  return { id: r.id, start_frame: +r.start_frame, end_frame: r.end_frame === '' ? null : +r.end_frame, type: r.type,
           memo: String(r.memo), author: String(r.author), created_at: String(r.created_at), status: String(r.status || 'todo'),
           mine: !!owner && String(r.owner) === String(owner) };
}

// ---------- 고객 페이지 ----------
function doGet(e) {
  var token = (e && e.parameter && e.parameter.p) || '';
  var p = project_(token);
  if (!p) {
    return HtmlService.createHtmlOutput('<p style="font-family:sans-serif;padding:40px;line-height:1.7">링크가 올바르지 않거나 더 이상 쓰지 않는 링크예요.<br>편집자에게 새 링크를 받아 주세요.</p>')
      .setTitle('영상 피드백');
  }
  var boot = JSON.stringify(clientProject(token)).replace(/</g, '\\u003c');   // 드라이브 API 키 포함 (공개 파일 재생 전용)
  return HtmlService.createHtmlOutput(CLIENT_HTML.replace('__BOOT__', boot))
    .setTitle(p.name + ' · 영상 피드백')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

function clientProject(token) {   // GitHub Pages 고객 페이지가 처음에 영상 정보를 받아 감
  var p = project_(token);
  if (!p) throw new Error('링크가 올바르지 않거나 더 이상 쓰지 않는 링크예요');
  return { token: String(p.token), name: String(p.name), drive_id: String(p.drive_id), fps: String(p.fps || '30/1'),
           key: props_().getProperty('DRIVE_KEY') || '' };
}

function clientLoad(token, owner) {
  if (!project_(token)) throw new Error('링크가 올바르지 않아요');
  var sh = book_().getSheetByName('feedback');
  var items = rows_(sh, F_COLS).filter(function (r) { return String(r.token) === String(token) && !r.deleted; })
    .map(function (r) { return clientRow_(r, owner); });
  return { items: items };
}

function clientAdd(token, it, owner) {
  if (!project_(token)) throw new Error('링크가 올바르지 않아요');
  it = it || {};
  var memo = String(it.memo || '').trim();
  if (!memo) throw new Error('내용이 비어 있어요');
  var sf = int_(it.start_frame), ef = int_(it.end_frame);
  if (sf === null) throw new Error('시간 정보가 없어요');
  if (ef !== null && ef <= sf) ef = null;
  var snap = typeof it.snapshot === 'string' && /^data:image\/jpeg;base64,/.test(it.snapshot) && it.snapshot.length < 49000 ? it.snapshot : '';
  return withLock_(function () {
    var sh = book_().getSheetByName('feedback');
    var mineCount = rows_(sh, F_COLS).filter(function (r) { return String(r.token) === String(token); }).length;
    if (mineCount >= 500) throw new Error('이 영상에 남길 수 있는 피드백 수(500개)를 넘었어요');
    var row = { id: newId_(), token: String(token), start_frame: sf, end_frame: ef === null ? '' : ef,
                type: TYPES.indexOf(it.type) >= 0 ? it.type : '기타', memo: text_(memo, 2000), author: text_(String(it.author || '고객').trim(), 40),
                owner: String(owner || '').slice(0, 60), created_at: now_(), updated_at: now_(), status: 'todo', deleted: '', snapshot: snap };
    sh.appendRow(F_COLS.map(function (c) { return cell_(row[c]); }));
    return clientRow_(row, owner);
  });
}

function findOwn_(sh, token, id, owner) {
  var r = rows_(sh, F_COLS).filter(function (x) { return String(x.token) === String(token) && String(x.id) === String(id) && !x.deleted; })[0];
  if (!r) throw new Error('피드백을 찾을 수 없어요');
  if (!owner || String(r.owner) !== String(owner)) throw new Error('내가 남긴 피드백만 바꿀 수 있어요');
  if ((r.status || 'todo') !== 'todo') throw new Error('이미 편집자가 확인한 피드백이에요');
  return r;
}

function clientUpdate(token, id, owner, memo) {
  memo = String(memo || '').trim();
  if (!memo) throw new Error('내용이 비어 있어요');
  return withLock_(function () {
    var sh = book_().getSheetByName('feedback');
    var r = findOwn_(sh, token, id, owner);
    setCells_(sh, r._row, F_COLS, { memo: text_(memo, 2000), updated_at: now_() });
    return true;
  });
}

function clientDelete(token, id, owner) {
  return withLock_(function () {
    var sh = book_().getSheetByName('feedback');
    var r = findOwn_(sh, token, id, owner);
    setCells_(sh, r._row, F_COLS, { deleted: 1, updated_at: now_(), snapshot: '' });
    return true;
  });
}

// ---------- 내 맥의 피드백 도구와 주고받기 ----------
function out_(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }

function doPost(e) {
  var req;
  try { req = JSON.parse(e.postData.contents); } catch (x) { return out_({ ok: false, error: '잘못된 요청이에요' }); }
  try {
    return out_(handle_(req));
  } catch (x) {
    return out_({ ok: false, error: String((x && x.message) || x) });
  }
}

var CLIENT_FNS = { clientProject: clientProject, clientLoad: clientLoad, clientAdd: clientAdd, clientUpdate: clientUpdate, clientDelete: clientDelete };

function handle_(req) {
  if (req.action === 'client') {   // 고객 페이지(GitHub Pages)에서 오는 요청 — 비밀키 없이 허용된 기능만
    var fn = CLIENT_FNS[req.fn];
    if (!fn) throw new Error('허용되지 않은 요청이에요');
    return { ok: true, result: fn.apply(null, (req.args || []).slice(0, 5)) };
  }
  var key = props_().getProperty('API_KEY');
  if (req.action === 'hello') {   // 처음 연결한 맥의 비밀키를 기억해요
    if (!req.key || String(req.key).length < 20) throw new Error('비밀키가 올바르지 않아요');
    if (!key) props_().setProperty('API_KEY', String(req.key));
    else if (key !== req.key) throw new Error('이미 다른 피드백 도구와 연결돼 있어요. Apps Script 편집기에서 resetConnection 을 실행한 뒤 다시 연결해 주세요');
    return { ok: true, version: VERSION, sheet_url: book_().getUrl() };
  }
  if (!key || key !== req.key) throw new Error('연결 비밀키가 맞지 않아요 — 검토 페이지에서 다시 연결해 주세요');

  if (req.action === 'settings') {   // 고객 페이지 재생용 드라이브 API 키
    if (req.drive_key) props_().setProperty('DRIVE_KEY', String(req.drive_key).slice(0, 80));
    return { ok: true };
  }

  if (req.action === 'project') {   // 고객 링크 만들기 (있으면 이름·영상만 갱신)
    if (!req.drive_id) throw new Error('드라이브 영상 ID가 없어요');
    return withLock_(function () {
      var sh = book_().getSheetByName('projects');
      var p = req.token ? rows_(sh, P_COLS).filter(function (x) { return String(x.token) === String(req.token); })[0] : null;
      var vals = { name: text_(req.name || '영상', 100), drive_id: String(req.drive_id), fps: String(req.fps || '30/1') };
      if (p) { setCells_(sh, p._row, P_COLS, vals); return { ok: true, token: String(p.token) }; }
      var token = Utilities.getUuid().replace(/-/g, '').slice(0, 24);
      sh.appendRow(P_COLS.map(function (c) { return cell_(c === 'token' ? token : c === 'created_at' ? now_() : vals[c]); }));
      return { ok: true, token: token };
    });
  }

  if (req.action === 'sync') {   // 상태 알려 주고 → 피드백 가져가기
    if (!project_(req.token)) throw new Error('고객 링크를 찾을 수 없어요 — 고객 링크를 다시 만들어 주세요');
    return withLock_(function () {
      var sh = book_().getSheetByName('feedback');
      var all = rows_(sh, F_COLS).filter(function (r) { return String(r.token) === String(req.token); });
      var byId = {};
      all.forEach(function (r) { byId[r.id] = r; });
      (req.updates || []).forEach(function (u) {
        var r = byId[u.id];
        if (r && STATUSES.indexOf(u.status) >= 0 && r.status !== u.status) {
          setCells_(sh, r._row, F_COLS, { status: u.status, updated_at: now_() });
          r.status = u.status;
        }
      });
      var known = {};
      (req.known || []).forEach(function (id) { known[id] = true; });
      var items = all.map(function (r) {
        var o = { id: r.id, start_frame: +r.start_frame, end_frame: r.end_frame === '' ? null : +r.end_frame, type: r.type,
                  memo: String(r.memo), author: String(r.author), created_at: String(r.created_at), status: String(r.status || 'todo'), deleted: !!r.deleted };
        if (!known[r.id] && r.snapshot && !r.deleted) o.snapshot = r.snapshot;
        return o;
      });
      return { ok: true, items: items };
    });
  }
  throw new Error('모르는 요청이에요: ' + req.action);
}

function resetConnection() {   // 다른 맥(피드백 도구)과 다시 연결할 때 실행
  props_().deleteProperty('API_KEY');
}

// ---------- 고객 페이지 화면 (자동 생성 — 고치지 마세요) ----------
var CLIENT_HTML = "<!DOCTYPE html>\n<html lang=\"ko\">\n<head>\n<meta charset=\"utf-8\">\n<base target=\"_top\">\n<style>\n:root{--bg:#0f1115;--panel:#161a22;--card:#1b2029;--line:#2a3140;--line2:#353d4f;--text:#e8ecf3;--sub:#a3acbd;--dim:#788297;--accent:#ff8a3d;--ok:#3ccf8e;--warn:#f5c84b;--danger:#ff5d5d;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}\n*{box-sizing:border-box}\nhtml,body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,\"Apple SD Gothic Neo\",\"Noto Sans KR\",sans-serif}\nbutton,input,textarea{font:inherit;color:inherit}\n.app{display:grid;grid-template-columns:minmax(0,1fr) 380px;min-height:100vh}\n.main{padding:16px 18px;display:flex;flex-direction:column;gap:10px;min-width:0}\n.panel{background:var(--panel);border-left:1px solid var(--line);padding:14px;display:flex;flex-direction:column;gap:10px;height:100vh;position:sticky;top:0}\n.top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}\n.title{font-size:16px;font-weight:700;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.who{display:flex;align-items:center;gap:6px;color:var(--sub);font-size:13px}\n.who input{width:130px;background:var(--card);border:1px solid var(--line2);border-radius:8px;padding:5px 8px}\n.stage{background:#000;border-radius:12px;overflow:hidden;position:relative}\nvideo{display:block;width:100%;max-height:68vh;background:#000}\n.banner{background:#3a2a12;border:1px solid #6b4a17;color:#ffd79a;border-radius:10px;padding:8px 12px;font-size:13px}\n.banner.info{background:#13283a;border-color:#24527a;color:#a9d4ff}\niframe.dp{display:block;width:100%;aspect-ratio:16/9;border:0;background:#000}\n.manual{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px;color:var(--sub)}\n.manual input{width:110px;background:var(--card);border:1px solid var(--line2);border-radius:8px;padding:6px 9px;font-family:var(--mono)}\n.banner.err{background:#3a1717;border-color:#7a2a2a;color:#ffb3b3}\n.posrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}\n.tc{font-family:var(--mono);font-size:17px;font-weight:700;letter-spacing:.5px}\n.btn{background:var(--card);border:1px solid var(--line2);border-radius:8px;padding:6px 10px;cursor:pointer;white-space:nowrap}\n.btn:hover{border-color:#4a5468}\n.btn.primary{background:var(--accent);border-color:var(--accent);color:#1a1006;font-weight:700}\n.btn.on{border-color:var(--accent);color:var(--accent)}\n.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:8px}\n.head{font-family:var(--mono);color:var(--sub);font-size:13px}\n.chips{display:flex;flex-wrap:wrap;gap:6px}\n.chip{border:1px solid var(--line2);border-radius:999px;padding:3px 11px;cursor:pointer;font-size:13px;background:var(--card)}\n.chip.sel{border-color:var(--c);color:var(--c);box-shadow:inset 0 0 0 1px var(--c)}\n.chip i{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--c);margin-right:5px;vertical-align:1px}\n.row{display:flex;gap:8px;align-items:stretch}\ntextarea{flex:1;min-height:64px;resize:vertical;background:var(--card);border:1px solid var(--line2);border-radius:10px;padding:9px 11px}\ntextarea:focus,input:focus{outline:none;border-color:var(--accent)}\n.state{font-size:12px;color:var(--dim);min-height:18px}\n.state.saved{color:var(--ok)}.state.error{color:var(--danger)}\n.keys{font-size:12px;color:var(--dim)}\n.keys kbd{font-family:var(--mono);background:var(--card);border:1px solid var(--line2);border-bottom-width:2px;border-radius:4px;padding:0 5px;font-size:11px;color:var(--sub)}\n.ph{display:flex;align-items:center;justify-content:space-between}\n.ph b{font-size:15px}\n.cnt{color:var(--sub);font-size:13px}\n.list{overflow:auto;display:flex;flex-direction:column;gap:8px;flex:1;padding-right:2px}\n.item{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--c);border-radius:10px;padding:9px 11px;cursor:pointer}\n.item:hover{border-color:#414a5f;border-left-color:var(--c)}\n.item.done{opacity:.6}\n.r1{display:flex;align-items:center;flex-wrap:wrap;gap:4px 6px;margin-bottom:3px}\n.t{font-family:var(--mono);font-weight:700;color:var(--c);font-size:13px;white-space:nowrap}\n.ty{font-size:11px;color:var(--c);border:1px solid var(--c);border-radius:999px;padding:0 7px;white-space:nowrap}\n.st{margin-left:auto;font-size:11px;font-weight:700;padding:0 7px;border-radius:999px;white-space:nowrap;background:#2a3140;color:var(--sub)}\n.st.done{background:#173a2a;color:var(--ok)}.st.question{background:#2b2142;color:#c9a8ff}.st.skipped{background:#3a3115;color:var(--warn)}\n.memo{white-space:pre-wrap;word-break:break-word}\n.meta{font-size:12px;color:var(--dim);margin-top:3px;display:flex;gap:8px;align-items:center}\n.meta button{background:none;border:none;color:var(--dim);cursor:pointer;padding:0 2px;font-size:12px}\n.meta button:hover{color:var(--text)}\n.empty{color:var(--dim);text-align:center;padding:30px 10px;line-height:1.8;white-space:pre-line}\n.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);background:#222836;border:1px solid var(--line2);border-radius:10px;padding:9px 16px;opacity:0;transition:.2s;pointer-events:none;z-index:9}\n.toast.show{opacity:1;transform:translateX(-50%)}\n.toast.err{border-color:var(--danger);color:#ffb3b3}\n@media (max-width:900px){.app{grid-template-columns:1fr}.panel{height:auto;position:static;border-left:none;border-top:1px solid var(--line)}.list{max-height:none}.keys{display:none}}\n</style>\n</head>\n<body>\n<div class=\"app\">\n  <main class=\"main\">\n    <div class=\"top\">\n      <div class=\"title\" id=\"title\">영상 피드백</div>\n      <label class=\"who\">이름 <input id=\"author\" maxlength=\"40\" placeholder=\"예: 김대리\"></label>\n    </div>\n    <div id=\"banner\" class=\"banner\" hidden></div>\n    <div class=\"stage\" id=\"stage\"><video id=\"v\" controls playsinline preload=\"auto\"></video></div>\n    <div class=\"manual\" id=\"manual\" hidden>\n      <label>시간 <input id=\"mStart\" placeholder=\"예: 1:23\"></label>\n      <label>~ 끝 (선택) <input id=\"mEnd\" placeholder=\"예: 1:30\"></label>\n      <span>플레이어 아래에 나오는 시간을 적어 주세요</span>\n    </div>\n    <div class=\"posrow\" id=\"posrow\">\n      <span class=\"tc\" id=\"tc\">00:00:00:00</span>\n      <button class=\"btn\" id=\"prevF\" title=\"1프레임 뒤로 (←)\">◀ 1프레임</button>\n      <button class=\"btn\" id=\"nextF\" title=\"1프레임 앞으로 (→)\">1프레임 ▶</button>\n      <span style=\"flex:1\"></span>\n      <button class=\"btn\" id=\"inBtn\" title=\"구간 시작 (I)\">⇤ 구간 시작</button>\n      <button class=\"btn\" id=\"outBtn\" title=\"구간 끝 (O)\">구간 끝 ⇥</button>\n    </div>\n    <div class=\"card\">\n      <div class=\"head\" id=\"head\">재생하다가 멈추고 피드백을 남겨 주세요</div>\n      <div class=\"chips\" id=\"chips\"></div>\n      <div class=\"row\">\n        <textarea id=\"memo\" placeholder=\"어떻게 고칠지 적어 주세요 (예: 여기 말 더듬는 부분 잘라 주세요)\"></textarea>\n        <button class=\"btn primary\" id=\"saveBtn\">남기기</button>\n      </div>\n      <div class=\"state\" id=\"state\"></div>\n    </div>\n    <div class=\"keys\"><kbd>Space</kbd> 재생·멈춤 · <kbd>←</kbd><kbd>→</kbd> 1프레임 (<kbd>Shift</kbd> 1초) · <kbd>M</kbd> 피드백 쓰기 · <kbd>Enter</kbd> 남기기 · <kbd>Shift</kbd>+<kbd>Enter</kbd> 줄바꿈 · <kbd>I</kbd><kbd>O</kbd> 구간 시작·끝 · <kbd>Esc</kbd> 취소</div>\n  </main>\n  <aside class=\"panel\">\n    <div class=\"ph\"><b>피드백</b><span class=\"cnt\" id=\"cnt\"></span></div>\n    <div class=\"list\" id=\"list\"></div>\n  </aside>\n</div>\n<div class=\"toast\" id=\"toast\"></div>\n<script>\nvar BOOT = __BOOT__;   // 구글 Apps Script 페이지면 영상 정보가 여기 들어오고, GitHub Pages면 null (주소에서 읽음)\nvar BACKEND = null;    // GitHub Pages: 피드백을 저장하는 구글 Apps Script 주소\nfunction backendCall(fn, args) {\n  return fetch(BACKEND, { method: 'POST', headers: { 'Content-Type': 'text/plain;charset=utf-8' }, body: JSON.stringify({ action: 'client', fn: fn, args: args }) })\n    .then(function (r) { return r.json(); })\n    .then(function (j) { if (!j.ok) throw new Error(j.error || '알 수 없는 오류'); return j.result; });\n}\nfunction main(BOOT) {\n  'use strict';\n  var TYPES = ['컷 삭제', '길이 조절', '순서 변경', '자막·텍스트 수정', '클립 교체', '기타'];\n  var COLORS = { '컷 삭제': '#ff5d5d', '길이 조절': '#f5c84b', '순서 변경': '#a77bff', '자막·텍스트 수정': '#4d8dff', '클립 교체': '#2ec5d3', '기타': '#ff7eb6' };\n  var STATUS = { todo: '확인 중', done: '반영됨 ✓', question: '확인 필요', skipped: '보류' };\n  var $ = function (s) { return document.querySelector(s); };\n  var v = $('#v'), memoEl = $('#memo');\n  var items = [], selType = null, draft = null, saving = false;\n\n  // 프레임 계산 (예: \"30000/1001\" = 29.97fps)\n  var fr = String(BOOT.fps || '30/1').split('/'), num = +fr[0] || 30, den = +fr[1] || 1;\n  var fd = den / num, base = Math.round(num / den);\n  function frameAt(t) { return Math.max(0, Math.floor((t || 0) / fd + 1e-4)); }\n  function frameTime(n) { return (n + 0.5) * fd; }\n  function pad(n) { return (n < 10 ? '0' : '') + n; }\n  function tcOf(n) { var f = n % base, s = Math.floor(n / base); return pad(Math.floor(s / 3600)) + ':' + pad(Math.floor(s % 3600 / 60)) + ':' + pad(s % 60) + ':' + pad(f); }\n\n  var store = {\n    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },\n    set: function (k, val) { try { localStorage.setItem(k, val); } catch (e) {} }\n  };\n  var owner = store.get('fbOwner');\n  if (!owner) { owner = 'o' + Math.random().toString(36).slice(2) + Date.now().toString(36); store.set('fbOwner', owner); }\n  $('#author').value = store.get('fbAuthor') || '';\n  $('#author').addEventListener('input', function () { store.set('fbAuthor', this.value.trim()); });\n\n  function call(fn) {   // google.script.run(Apps Script 페이지) 또는 fetch(GitHub Pages) 를 Promise로\n    var args = Array.prototype.slice.call(arguments, 1);\n    if (!(window.google && google.script && google.script.run)) return backendCall(fn, args);\n    return new Promise(function (res, rej) {\n      var r = google.script.run.withSuccessHandler(res).withFailureHandler(function (e) { rej(new Error((e && e.message) || String(e))); });\n      r[fn].apply(r, args);\n    });\n  }\n  function toast(msg, err) {\n    var t = $('#toast'); t.textContent = msg; t.className = 'toast show' + (err ? ' err' : '');\n    clearTimeout(toast._t); toast._t = setTimeout(function () { t.className = 'toast'; }, err ? 3500 : 2000);\n  }\n  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }\n\n  // ---------- 영상 ----------\n  $('#title').textContent = BOOT.name;\n  document.title = BOOT.name + ' · 영상 피드백';\n  // 재생: 드라이브 API(키가 있을 때) → 프레임 단위로 정확. 안 되면 드라이브 기본 플레이어 + 시간 직접 입력\n  var manual = false;\n  function useDrivePlayer(reason) {\n    if (manual) return;\n    manual = true;\n    var f = document.createElement('iframe'); f.className = 'dp'; f.allow = 'autoplay; fullscreen'; f.setAttribute('allowfullscreen', '');\n    f.src = 'https://drive.google.com/file/d/' + encodeURIComponent(BOOT.drive_id) + '/preview';\n    v.removeAttribute('src'); v.load(); v.replaceWith(f);\n    $('#posrow').hidden = true; $('#manual').hidden = false;\n    var b = $('#banner'); b.className = 'banner info'; b.hidden = false;\n    b.textContent = '드라이브 플레이어로 재생해요. 고칠 곳의 시간을 \"시간\" 칸에 적고 피드백을 남겨 주세요.' + (reason ? ' (' + reason + ')' : '');\n    document.querySelector('.keys').hidden = true;\n  }\n  if (BOOT.key) {\n    v.crossOrigin = 'anonymous';   // 화면 캡처를 같이 보내려고\n    v.addEventListener('error', function () { useDrivePlayer('정확한 재생을 불러오지 못했어요'); });\n    v.src = 'https://www.googleapis.com/drive/v3/files/' + encodeURIComponent(BOOT.drive_id) + '?alt=media&key=' + encodeURIComponent(BOOT.key);\n  } else {\n    useDrivePlayer();\n  }\n  function parseTime(str) {   // \"1:23\", \"01:02:03\", \"83\", \"00:01:23:15\"(프레임) → 프레임 번호\n    str = String(str || '').trim(); if (!str) return null;\n    var p = str.split(/[:;.]/).map(function (x) { return x === '' ? NaN : +x; });\n    if (p.some(isNaN) || p.length > 4) return NaN;\n    var sec, fr = 0;\n    if (p.length === 4) { sec = p[0] * 3600 + p[1] * 60 + p[2]; fr = p[3]; }\n    else if (p.length === 3) sec = p[0] * 3600 + p[1] * 60 + p[2];\n    else if (p.length === 2) sec = p[0] * 60 + p[1];\n    else sec = p[0];\n    return p.length === 4 ? sec * base + fr : Math.floor(sec / fd + 1e-4);   // 타임코드는 그대로, 분:초는 실제 시간 → 프레임\n  }\n\n  function tick() {\n    if (!manual) $('#tc').textContent = tcOf(frameAt(v.currentTime));\n    requestAnimationFrame(tick);\n  }\n  function stepBy(n) { v.pause(); v.currentTime = frameTime(Math.max(0, frameAt(v.currentTime) + n)); }\n  function seekSec(s) { v.pause(); v.currentTime = Math.max(0, Math.min((v.duration || 1e9) - fd, v.currentTime + s)); }\n  $('#prevF').onclick = function () { stepBy(-1); };\n  $('#nextF').onclick = function () { stepBy(1); };\n\n  // ---------- 피드백 쓰기 ----------\n  function startDraft() { if (manual) return; if (!draft) { v.pause(); draft = { start: frameAt(v.currentTime), end: null }; } renderHead(); }\n  function setIn() { v.pause(); draft = { start: frameAt(v.currentTime), end: draft && draft.end != null && draft.end > frameAt(v.currentTime) ? draft.end : null }; renderHead(); toast('구간 시작 ' + tcOf(draft.start)); }\n  function setOut() {\n    v.pause(); var f = frameAt(v.currentTime);\n    if (!draft) draft = { start: f, end: null };\n    if (f <= draft.start) { toast('구간 끝은 시작보다 뒤여야 해요', true); return; }\n    draft.end = f; renderHead(); toast('구간 끝 ' + tcOf(f));\n  }\n  $('#inBtn').onclick = setIn; $('#outBtn').onclick = setOut;\n  function renderHead() {\n    var h = $('#head');\n    if (!draft) { h.textContent = '재생하다가 멈추고 피드백을 남겨 주세요'; return; }\n    h.textContent = tcOf(draft.start) + (draft.end != null ? ' ~ ' + tcOf(draft.end) : '') + ' 에 남김';\n  }\n  function renderChips() {\n    var box = $('#chips'); box.innerHTML = '';\n    TYPES.forEach(function (t) {\n      var c = el('span', 'chip' + (selType === t ? ' sel' : '')); c.style.setProperty('--c', COLORS[t]);\n      c.appendChild(el('i')); c.appendChild(document.createTextNode(t));\n      c.onclick = function () { selType = selType === t ? null : t; renderChips(); memoEl.focus(); };\n      box.appendChild(c);\n    });\n  }\n  memoEl.addEventListener('focus', function () { startDraft(); });\n  memoEl.addEventListener('input', function () { store.set('fbDraft:' + BOOT.token, memoEl.value); setState(memoEl.value ? 'draft' : ''); });\n  memoEl.value = store.get('fbDraft:' + BOOT.token) || '';\n  memoEl.addEventListener('keydown', function (e) {\n    e.stopPropagation();\n    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); save(); }\n    if (e.key === 'Escape') { memoEl.blur(); }\n  });\n  $('#saveBtn').onclick = function () { save(); };\n  function setState(st, msg) {\n    var s = $('#state'), now = new Date().toTimeString().slice(0, 8);\n    var map = { '': ['', ''], draft: ['', '✎ 작성 중'], saving: ['', '저장 중…'], saved: ['saved', '✓ 저장됨 ' + now], error: ['error', '⚠ 저장 실패 — ' + (msg || '') + ' (다시 눌러 주세요)'] };\n    var m = map[st] || map['']; s.className = 'state ' + m[0]; s.textContent = m[1];\n  }\n  function snapshot() {   // 고친 곳 화면 캡처 (작게) — 드라이브가 막으면 없이 보냄\n    if (manual) return null;\n    try {\n      if (!v.videoWidth || v.readyState < 2) return null;\n      var tries = [[480, 0.6], [360, 0.5], [240, 0.45]];\n      for (var i = 0; i < tries.length; i++) {\n        var w = Math.min(tries[i][0], v.videoWidth), hgt = Math.round(v.videoHeight * w / v.videoWidth);\n        var c = document.createElement('canvas'); c.width = w; c.height = hgt;\n        c.getContext('2d').drawImage(v, 0, 0, w, hgt);\n        var url = c.toDataURL('image/jpeg', tries[i][1]);\n        if (url.length < 45000) return url;\n      }\n    } catch (e) {}\n    return null;\n  }\n  function save() {\n    var memo = memoEl.value.trim();\n    if (!memo) { toast('내용을 적어 주세요', true); memoEl.focus(); return; }\n    if (saving) return;\n    var author = $('#author').value.trim();\n    if (!author) { toast('이름을 먼저 적어 주세요 (오른쪽 위)', true); $('#author').focus(); return; }\n    if (manual) {   // 드라이브 플레이어 모드: 적은 시간 사용\n      var sf = parseTime($('#mStart').value), ef = parseTime($('#mEnd').value);\n      if (sf === null || isNaN(sf)) { toast('고칠 곳의 시간을 적어 주세요 (예: 1:23)', true); $('#mStart').focus(); return; }\n      if (isNaN(ef)) { toast('끝 시간 형식을 확인해 주세요 (예: 1:30)', true); $('#mEnd').focus(); return; }\n      draft = { start: sf, end: ef !== null && ef > sf ? ef : null };\n    }\n    startDraft();\n    var it = { start_frame: draft.start, end_frame: draft.end, type: selType || '기타', memo: memo, author: author, snapshot: snapshot() };\n    saving = true; setState('saving');\n    call('clientAdd', BOOT.token, it, owner).then(function (row) {\n      saving = false; setState('saved');\n      items.push(row); render();\n      memoEl.value = ''; store.set('fbDraft:' + BOOT.token, ''); draft = null; $('#mStart').value = ''; $('#mEnd').value = ''; selType = null; renderChips(); renderHead(); memoEl.blur();\n      toast('남겼어요');\n    }).catch(function (e) { saving = false; setState('error', e.message); });\n  }\n\n  // ---------- 목록 ----------\n  function render() {\n    items.sort(function (a, b) { return a.start_frame - b.start_frame || (a.created_at < b.created_at ? -1 : 1); });\n    var list = $('#list'); list.innerHTML = '';\n    var left = items.filter(function (x) { return (x.status || 'todo') === 'todo'; }).length;\n    $('#cnt').textContent = '전체 ' + items.length + (left ? ' · 확인 중 ' + left : '');\n    if (!items.length) { list.appendChild(el('div', 'empty', '아직 남긴 피드백이 없어요.\\n영상을 보다가 고칠 곳에서 멈추고 피드백을 적어 주세요.')); return; }\n    items.forEach(function (it) {\n      var st = it.status || 'todo';\n      var box = el('div', 'item' + (st === 'done' ? ' done' : '')); box.style.setProperty('--c', COLORS[it.type] || COLORS['기타']);\n      var r1 = el('div', 'r1');\n      r1.appendChild(el('span', 't', tcOf(+it.start_frame) + (it.end_frame !== '' && it.end_frame != null ? ' ~ ' + tcOf(+it.end_frame) : '')));\n      r1.appendChild(el('span', 'ty', it.type || '기타'));\n      r1.appendChild(el('span', 'st ' + st, STATUS[st] || st));\n      box.appendChild(r1);\n      box.appendChild(el('div', 'memo', it.memo));\n      var meta = el('div', 'meta'); meta.appendChild(el('span', '', it.author || ''));\n      if (it.mine && st === 'todo') {\n        var ed = el('button', '', '수정'); ed.onclick = function (e) { e.stopPropagation(); edit(it); };\n        var del = el('button', '', '삭제'); del.onclick = function (e) { e.stopPropagation(); remove(it); };\n        meta.appendChild(ed); meta.appendChild(del);\n      }\n      box.appendChild(meta);\n      box.onclick = function () { if (manual) return; v.pause(); v.currentTime = frameTime(+it.start_frame); };\n      list.appendChild(box);\n    });\n  }\n  function edit(it) {\n    var memo = prompt('피드백 수정', it.memo);\n    if (memo == null || !memo.trim() || memo.trim() === it.memo) return;\n    call('clientUpdate', BOOT.token, it.id, owner, memo.trim()).then(function () { it.memo = memo.trim(); render(); toast('수정했어요'); })\n      .catch(function (e) { toast('수정하지 못했어요: ' + e.message, true); });\n  }\n  function remove(it) {\n    if (!confirm('이 피드백을 지울까요?')) return;\n    call('clientDelete', BOOT.token, it.id, owner).then(function () { items = items.filter(function (x) { return x.id !== it.id; }); render(); toast('지웠어요'); })\n      .catch(function (e) { toast('지우지 못했어요: ' + e.message, true); });\n  }\n  function load() {\n    return call('clientLoad', BOOT.token, owner).then(function (res) { items = res.items || []; render(); })\n      .catch(function (e) { toast('목록을 불러오지 못했어요: ' + e.message, true); });\n  }\n\n  // ---------- 단축키 ----------\n  document.addEventListener('keydown', function (e) {\n    var tag = (e.target && e.target.tagName) || '';\n    if (tag === 'INPUT' || tag === 'TEXTAREA' || e.metaKey || e.ctrlKey || e.altKey) return;\n    if (manual) { if (e.code === 'KeyM') { e.preventDefault(); $('#mStart').focus(); } return; }\n    var done = function () { e.preventDefault(); };\n    switch (e.code) {\n      case 'Space': done(); if (v.paused) v.play(); else v.pause(); break;\n      case 'ArrowLeft': done(); if (e.shiftKey) seekSec(-1); else stepBy(-1); break;\n      case 'ArrowRight': done(); if (e.shiftKey) seekSec(1); else stepBy(1); break;\n      case 'KeyM': done(); startDraft(); memoEl.focus(); break;\n      case 'KeyI': done(); setIn(); break;\n      case 'KeyO': done(); setOut(); break;\n      case 'Escape': done(); draft = null; renderHead(); break;\n    }\n  }, true);\n\n  renderChips(); renderHead(); load(); requestAnimationFrame(tick);\n  if (memoEl.value) setState('draft');\n  setInterval(function () { if (!document.hidden) load(); }, 30000);   // 편집자가 반영하면 상태가 바뀌어요\n}\n\nfunction fatal(msg) {\n  document.querySelector('.app').innerHTML = '<p style=\"padding:40px;line-height:1.7;color:#a3acbd\"></p>';\n  document.querySelector('.app p').textContent = msg;\n}\n(function start() {\n  if (BOOT) return main(BOOT);\n  var q = new URLSearchParams(location.search), p = q.get('p') || '', s = q.get('s') || window.FB_BACKEND || '';\n  var local = /^(localhost|127\\.0\\.0\\.1)$/.test(location.hostname);\n  if (/^[A-Za-z0-9_-]{20,}$/.test(s)) BACKEND = 'https://script.google.com/macros/s/' + s + '/exec';\n  else if (/^https:\\/\\/script\\.google\\.com\\/macros\\/s\\/[A-Za-z0-9_-]+\\/exec$/.test(s) || (local && /^http:\\/\\/127\\.0\\.0\\.1:\\d+\\/exec$/.test(s))) BACKEND = s;\n  if (!/^[A-Za-z0-9]{10,40}$/.test(p) || !BACKEND) return fatal('링크가 올바르지 않아요. 편집자에게 받은 주소 전체를 다시 열어 주세요.');\n  document.getElementById('title').textContent = '불러오는 중…';\n  backendCall('clientProject', [p]).then(main).catch(function (e) { fatal('영상 정보를 불러오지 못했어요 — ' + e.message); });\n})();\n</script>\n</body>\n</html>\n";
