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
  if (!Array.isArray(remotes) || !remotes.length) return;
  [transferSelect, detectSelect].forEach((select) => {
    if (!select) return;
    const oldValue = select.value;
    select.replaceChildren(...remotes.map((name) => {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name;
      return option;
    }));
    select.value = remotes.includes(oldValue) ? oldValue : remotes[0];
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

// 빈 값은 서버의 실제 PATH/BookOasis 폴백을 확인한 뒤 명시적인 기본 경로로 채운다.
if ((binaryInput && !binaryInput.value.trim()) || (configInput && !configInput.value.trim())) {
  checkRclone('config', {quiet: true});
}
