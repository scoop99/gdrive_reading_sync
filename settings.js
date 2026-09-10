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

// 테스트용 export (반드시 이 형태). new Function 스코프에는 module 이 없어
// typeof module 이 'undefined' 로 평가된다 — 단락 평가라 브라우저에서 안전하다.
// CJS require 로 로드되면 여기서 export 하고 즉시 return 해 아래 DOM 코드가
// 실행되지 않게 한다 (test_settings_js.js 에서 순수 함수만 테스트한다).
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { pickRemoteOptions };
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
