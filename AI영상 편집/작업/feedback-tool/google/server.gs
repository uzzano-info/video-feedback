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

  if (req.action === 'projects') {   // 영상 목록 (편집자 홈 · 맥 도구가 가져감)
    var ss = book_();
    var fb = rows_(ss.getSheetByName('feedback'), F_COLS).filter(function (r) { return !r.deleted; });
    var list = rows_(ss.getSheetByName('projects'), P_COLS).map(function (p) {
      var mine = fb.filter(function (r) { return String(r.token) === String(p.token); });
      return { token: String(p.token), name: String(p.name), drive_id: String(p.drive_id), fps: String(p.fps), created_at: String(p.created_at),
               total: mine.length, todo: mine.filter(function (r) { return (r.status || 'todo') === 'todo'; }).length };
    });
    return { ok: true, projects: list };
  }

  if (req.action === 'setstatus') {   // 편집자 홈에서 상태 바꾸기
    if (STATUSES.indexOf(req.status) < 0) throw new Error('상태 값이 올바르지 않아요');
    return withLock_(function () {
      var sh = book_().getSheetByName('feedback');
      var r = rows_(sh, F_COLS).filter(function (x) { return String(x.token) === String(req.token) && String(x.id) === String(req.id); })[0];
      if (!r) throw new Error('피드백을 찾을 수 없어요');
      setCells_(sh, r._row, F_COLS, { status: req.status, updated_at: now_() });
      return { ok: true };
    });
  }

  if (req.action === 'project') {   // 고객 링크 만들기 (있으면 이름·영상만 갱신, 같은 영상·이름이면 그 링크 재사용)
    if (!req.drive_id) throw new Error('드라이브 영상 ID가 없어요');
    return withLock_(function () {
      var sh = book_().getSheetByName('projects');
      var all = rows_(sh, P_COLS);
      var p = req.token ? all.filter(function (x) { return String(x.token) === String(req.token); })[0]
        : all.filter(function (x) { return String(x.drive_id) === String(req.drive_id) && String(x.name) === String(req.name || '영상'); })[0];
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
