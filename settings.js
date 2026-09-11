// P11 — 셀렉트에 넣을 옵션 목록과 선택값을 정한다. DOM 을 만지지 않는 순수 함수.
//
// 규칙:
//  - 저장값이 있으면 **무슨 일이 있어도 그 값을 선택**한다. 목록 조회가 실패했거나
//    목록에 없더라도 옵션으로 만들어 유지한다 (missing: true 로 표시).
//  - 저장값이 없을 때만 목록의 첫 항목을 기본값으로 쓴다.
//  - 저장값을 다른 값으로 **갈아치우지 않는다.** 그게 이 버그의 본질이다.
//
// @param {string[]} remotes 조회된 리모트 이름 목록 (실패 시 빈 배열)
// @param {string}   saved   저장된 선택값 ('' 가능)
// @returns {{options: {value: string, missing: boolean}[], selected: string}}
function pickRemoteOptions(remotes, saved) {
  const list = Array.isArray(remotes) ? remotes.filter(Boolean).map(String) : [];
  const cur = String(saved || '').trim();
  const options = list.map((value) => ({ value, missing: false }));
  if (cur && !list.includes(cur)) {
    // 저장값이 목록에 없다 — 버리지 말고 맨 앞에 살려 둔다.
    options.unshift({ value: cur, missing: true });
  }
  const selected = cur || (list.length ? list[0] : '');
  return { options, selected };
}

/**
 * 탐색기에서 고른 폴더를 각 설정칸에 넣을 최종 값으로 바꾼다.
 *
 * 본체 탐색 API 가 디렉터리만 돌려주므로(browse_routes.py:126) 파일은 고를 수 없다.
 * RCLONE_BIN / RCLONE_CONFIG 는 파일명이 고정이라 폴더 뒤에 붙여 완성한다.
 *
 * @param {string} key    설정 키
 * @param {string} folder 사용자가 고른 폴더 절대경로
 * @param {boolean} isWindows 경로 구분자/실행파일명 판단
 * @returns {string} 입력칸에 넣을 값
 */
function resolvePickedPath(key, folder, isWindows) {
  const dir = String(folder || '').trim().replace(/[\\/]+$/, '');
  if (!dir) return '';
  const sep = isWindows ? '\\' : '/';
  const FILE_BY_KEY = {
    RCLONE_BIN: isWindows ? 'rclone.exe' : 'rclone',
    RCLONE_CONFIG: 'rclone.conf',
  };
  const fname = FILE_BY_KEY[key];
  return fname ? `${dir}${sep}${fname}` : dir;
}

// 테스트용 export (반드시 이 형태). new Function 스코프에는 module 이 없어
// typeof module 이 'undefined' 로 평가된다 — 단락 평가라 브라우저에서 안전하다.
// CJS require 로 로드되면 여기서 export 하고 즉시 return 해 아래 DOM 코드가
// 실행되지 않게 한다 (test_settings_js.js 에서 순수 함수만 테스트한다).
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { pickRemoteOptions, resolvePickedPath };
  return;
}

const endpoint = `/api/webhook/${pluginId}/rclone-check`;
const field = (name) => root.querySelector(`[name="${name}"]`);
const binaryInput = field('RCLONE_BIN');
const configInput = field('RCLONE_CONFIG');
const transferSelect = field('TRANSFER_REMOTE');
const detectSelect = field('DETECT_REMOTE');

function showResult(kind, message, state) {
  const el = root.querySelector(`[data-result="${kind}"]`);
  if (!el) return;
  el.textContent = message || '';
  el.className = `gdrs-settings-result${state ? ` is-${state}` : ''}`;
}

function setRemoteOptions(remotes) {
  [
    [transferSelect, 'TRANSFER_REMOTE'],
    [detectSelect, 'DETECT_REMOTE'],
  ].forEach(([select, key]) => {
    if (!select) return;
    // 저장값이 정본이다. 이미 셀렉트에 선택된 값이 있으면 그걸, 없으면 config 의 값을 쓴다.
    const saved = String(select.value || (config && config[key]) || '').trim();
    const { options, selected } = pickRemoteOptions(remotes, saved);
    select.replaceChildren(
      ...options.map((o) => {
        const el = document.createElement('option');
        el.value = o.value;
        el.textContent = o.missing ? `${o.value} (설정값 — 목록에 없음)` : o.value;
        return el;
      })
    );
    select.value = selected;
  });
}

async function checkRclone(mode, options = {}) {
  const btn = root.querySelector(`[data-check="${mode}"]`);
  const quiet = !!options.quiet;
  if (btn) btn.disabled = true;
  if (!quiet) showResult(mode, '확인 중…', 'warn');
  try {
    const response = await fetch(endpoint, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        mode,
        rclone_bin: binaryInput ? binaryInput.value.trim() : '',
        rclone_config: configInput ? configInput.value.trim() : '',
      }),
    });
    const data = await response.json().catch(() => ({success: false, error: `HTTP ${response.status}`}));
    if (response.status === 404) {
      throw new Error('확인 기능이 서버에 아직 등록되지 않았습니다. BookOasis를 재시작한 뒤 다시 확인해 주세요.');
    }
    if (!data.success) throw new Error(data.error || `HTTP ${response.status}`);

    if (binaryInput && !binaryInput.value.trim() && data.rclone_resolved) binaryInput.value = data.rclone_resolved;
    if (mode === 'config') {
      if (configInput && !configInput.value.trim() && data.config_file) configInput.value = data.config_file;
      setRemoteOptions(data.remotes);
      const count = Array.isArray(data.remotes) ? data.remotes.length : 0;
      showResult('config', count
        ? `사용 가능 · Google Drive 리모트 ${count}개: ${data.remotes.join(', ')}`
        : '설정 파일은 읽었지만 Google Drive 리모트가 없습니다.', count ? 'ok' : 'warn');
    } else {
      showResult('binary', `사용 가능 · ${data.rclone_version} · ${data.rclone_resolved}`, 'ok');
    }
    return data;
  } catch (error) {
    showResult(mode, `사용 불가 · ${error.message || error}`, 'error');
    return null;
  } finally {
    if (btn) btn.disabled = false;
  }
}

root.querySelectorAll('[data-check]').forEach((button) => {
  button.addEventListener('click', () => checkRclone(button.dataset.check));
});
[binaryInput, configInput].forEach((input) => {
  if (input) input.addEventListener('input', () => {
    showResult(input === binaryInput ? 'binary' : 'config', '', '');
  });
});

// ---- P12 — 설정 경로 탐색기 ---------------------------------------------
// 본체 탐색 API(GET /api/media/browse-paths)를 그대로 호출한다. 권한 검사·허용 루트·
// 경로 탈출 방지는 본체가 이미 한다(browse_routes.py). 우리는 UI 만 만든다.
// 본체 모달은 결과를 library-form-path 에 하드코딩하므로 재사용 불가 → 우리 모달을 쓴다.
const BROWSE_API = '/api/media/browse-paths';
// 파일명을 자동으로 붙일 설정 키. 본체 API 가 디렉터리만 주므로(browse_routes.py:126)
// 이 두 칸은 폴더를 고르면 고정 파일명을 뒤에 붙여 완성한다.
const BROWSE_FILE_KEYS = { RCLONE_BIN: true, RCLONE_CONFIG: true };

const browseModal = root.querySelector('#gdrs-browse-modal');
const browseList = root.querySelector('#gdrs-browse-list');
const browseCur = root.querySelector('#gdrs-browse-cur');
const browsePreview = root.querySelector('#gdrs-browse-preview');
const browseTitle = root.querySelector('#gdrs-browse-title');

let browseKey = '';    // 지금 탐색 중인 설정 키
let browsePath = '';   // 현재 폴더 (서버 응답의 currentPath 원문)

// 고른 경로 자체로 OS 를 판정한다. navigator 보다 정확하다 — 서버가 리눅스 도커면
// 경로가 /mnt/... 로 오므로 platform 보다 경로가 더 신뢰할 만하다 (설계 §2).
function browseIsWindows(p) {
  const s = String(p || '');
  return /^[A-Za-z]:/.test(s) || s.includes('\\');
}

// 파일칸이면 파일명을 떼고 상위 폴더에서 시작한다. 폴더칸이면 입력값 그대로. 비면 루트.
function browseStartPath(key, value) {
  const v = String(value || '').trim();
  if (!v) return '';
  if (BROWSE_FILE_KEYS[key]) {
    const i = Math.max(v.lastIndexOf('/'), v.lastIndexOf('\\'));
    return i > 0 ? v.slice(0, i) : '';
  }
  return v;
}

// 확정 시 입력칸에 들어갈 최종 값을 미리 보여준다 — 파일칸이면 파일명이 붙는 걸 눈으로
// 확인시켜 "왜 파일이 안 보이지?" 를 원천 차단한다 (설계 §3.3).
function browseRenderPreview() {
  if (!browsePreview) return;
  if (!browsePath) {
    browsePreview.textContent = '';
    return;
  }
  const finalValue = resolvePickedPath(browseKey, browsePath, browseIsWindows(browsePath));
  browsePreview.textContent = finalValue ? `선택하면 입력칸에 들어갈 값: ${finalValue}` : '';
}

function browseRenderError(message) {
  if (!browseList) return;
  browseList.replaceChildren();
  const div = document.createElement('div');
  div.className = 'gdrs-browse-error';
  div.textContent = message || '목록을 불러오지 못했습니다.';
  browseList.appendChild(div);
}

async function browse(path) {
  if (!browseList) return;
  browseList.textContent = '불러오는 중…';
  try {
    const url = `${BROWSE_API}?path=${encodeURIComponent(path || '')}`;
    const resp = await fetch(url, { credentials: 'same-origin' });
    const data = await resp.json().catch(() => ({ success: false, error: `HTTP ${resp.status}` }));
    if (!data.success) {
      // 본체 API 실패를 조용히 삼키지 않는다 — 403(허용 루트 밖) 등은 사용자가 알아야 조치한다.
      browsePath = '';
      if (browseCur) browseCur.textContent = '–';
      browseRenderError(data.error || `HTTP ${resp.status}`);
      browseRenderPreview();
      return;
    }
    browsePath = data.currentPath || '';
    if (browseCur) browseCur.textContent = browsePath || '(드라이브 루트)';
    browseList.replaceChildren();
    const items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) {
      const div = document.createElement('div');
      div.className = 'gdrs-browse-empty';
      div.textContent = '하위 폴더가 없습니다. [이 폴더 선택] 을 누르세요.';
      browseList.appendChild(div);
    } else {
      items.forEach((item) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'gdrs-browse-item';
        btn.textContent = item.name;
        btn.addEventListener('click', () => browse(item.path));
        browseList.appendChild(btn);
      });
    }
    browseRenderPreview();
  } catch (error) {
    browsePath = '';
    browseRenderError(`탐색 실패 · ${error.message || error}`);
    browseRenderPreview();
  }
}

function openBrowse(key) {
  const input = field(key);
  browseKey = key;
  if (browseTitle) {
    browseTitle.textContent = BROWSE_FILE_KEYS[key]
      ? '폴더 선택 — 파일명이 자동으로 붙습니다'
      : '폴더 선택';
  }
  if (browseModal) browseModal.hidden = false;
  browse(browseStartPath(key, input ? input.value : ''));
}

function closeBrowse() {
  if (browseModal) browseModal.hidden = true;
  browseKey = '';
  browsePath = '';
}

function acceptBrowse() {
  if (!browseKey) {
    closeBrowse();
    return;
  }
  const input = field(browseKey);
  if (input && browsePath) {
    input.value = resolvePickedPath(browseKey, browsePath, browseIsWindows(browsePath));
    // input 이벤트를 dispatch 해 기존 checkRclone 결과 표시를 초기화한다 (설계 §4).
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  closeBrowse();
}

root.querySelectorAll('[data-browse]').forEach((button) => {
  button.addEventListener('click', () => openBrowse(button.dataset.browse));
});
const browseClose = root.querySelector('#gdrs-browse-close');
const browseCancel = root.querySelector('#gdrs-browse-cancel');
const browseOk = root.querySelector('#gdrs-browse-ok');
if (browseClose) browseClose.addEventListener('click', closeBrowse);
if (browseCancel) browseCancel.addEventListener('click', closeBrowse);
if (browseOk) browseOk.addEventListener('click', acceptBrowse);
if (browseModal) {
  // 배경(모달 자신) 클릭 시 취소 — 입력칸은 그대로.
  browseModal.addEventListener('click', (ev) => {
    if (ev.target === browseModal) closeBrowse();
  });
}
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && browseModal && !browseModal.hidden) closeBrowse();
});

// P11 — 초기화 두 단계.
// 1단계 — 즉시. 저장값으로 옵션을 만들어 선택한다.
//   본체 applyConfigValues 는 option 이 없는 select 에 값을 못 넣는다
//   (plugins.js:215). 여기서 복원하지 않으면, 사용자가 화면을 열고 저장만 눌러도
//   빈 문자열이 저장돼 동기화가 멈춘다 (plugins.js:324 가 select 를 그대로 긁는다).
setRemoteOptions([]);

// 2단계 — 조용히 실제 목록을 채운다. 조건 없이 항상.
//   기존에는 RCLONE_BIN/RCLONE_CONFIG 가 비었을 때만 조회해서, 첫 저장 이후로는
//   목록이 영영 비어 있었다.
checkRclone('config', { quiet: true });
