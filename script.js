(function () {
  // 종결 상태 — 더 이상 처리되지 않는다. 진행률의 분자.
  const TERMINAL_STATUSES = ["completed", "skipped", "failed"];
  // 아직 남은 상태.
  const PENDING_STATUSES = ["queued", "retry", "processing", "dry_run"];

  /**
   * 진행률 계산. DOM 을 만지지 않는 순수 함수 — test_script_js.js 가 이걸 검증한다.
   *
   * 구 화면은 `전체 N건 · 완료 M` 이었는데 `완료` 가 status='completed' 만 세서,
   * 중복 판정으로 끝난 수십만 건이 진행에 반영되지 않았다. 실제 84% 진행을
   * 1% 처럼 보이게 했다. 여기서는 **종결(완료+중복+실패) / 전체** 로 낸다.
   *
   * statusCounts 가 없으면(구 응답 호환) completed 만으로 계산한다.
   */
  function computeProgress(summary, statusCounts) {
    const sm = summary || {};
    const counts = statusCounts || {};
    const at = (k) => Number(counts[k] || 0);
    const total = Number(sm.all || 0);
    const known = Object.keys(counts).length > 0;
    const done = known
      ? TERMINAL_STATUSES.reduce((a, k) => a + at(k), 0)
      : Number(sm.completed || 0);
    const left = known
      ? PENDING_STATUSES.reduce((a, k) => a + at(k), 0)
      : Math.max(0, total - done);
    const pct = total > 0 ? (done / total) * 100 : 0;
    return { total, done, left, pct };
  }

  // 테스트용 export. new Function / 브라우저 스코프에는 module 이 없어
  // typeof 검사에서 단락 평가된다 (settings.js 와 같은 형태).
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { computeProgress };
    return;
  }

  const PLUGIN_ID = "gdrive_reading_sync";
  const API_STATUS = `/api/webhook/${PLUGIN_ID}/status`;
  const API_JOBS = `/api/webhook/${PLUGIN_ID}/jobs`;
  const API_PREFLIGHT = `/api/webhook/${PLUGIN_ID}/preflight`;
  const API_RETRY = `/api/webhook/${PLUGIN_ID}/retry`;
  const API_CLEANUP = `/api/webhook/${PLUGIN_ID}/cleanup`;

  const $ = (id) => document.getElementById(id);

  // §9.1 / S7 — 서버측 상태 모델. 클라이언트는 절대 자체 필터링을 하지 않는다.
  const state = {
    page: 1,
    pageSize: 100,
    status: "",
    action: "",
    result: "",
    search: "",
    order: "desc",
    pages: 0,
    total: 0,
    selected: new Set(),
    scans: {},   // P9 — /jobs 의 `scans` (folder 경로 -> scan row dict)
  };

  const STATUS_LABEL = {
    dry_run: "감지됨",
    queued: "버퍼",
    retry: "재시도",
    processing: "처리중",
    completed: "완료",
    skipped: "건너뜀",
    failed: "실패",
  };

  const ACTION_LABEL = {
    create: "생성",
    edit: "수정",
    move: "이동",
    rename: "이름 변경",
    delete: "삭제",
    restore: "복원",
  };

  const RESULT_LABEL = {
    copied: "복사완료",
    copied_size_only: "복사(크기만)",
    duplicate: "중복",
    renamed_event: "이름 변경",
    renamed_cold_start: "초기 이름 변경",
    renamed_directory: "폴더 이름 변경",
    remote_deleted: "원격삭제",
    max_attempts_exceeded: "시도초과",
    recovery_limit_exceeded: "복구초과",
    recovered_tmp_cleanup_failed: "tmp삭제실패",
    bad_config: "bad_config",
    bad_path: "bad_path",
    recovered: "복구",
    rename_conflict: "충돌",
    rename_source_missing: "원본부재",
    rename_candidates_multiple: "복수후보",
    safety_conflict: "충돌(안전)",
  };

  function fmtBytes(n) {
    const v = Number(n || 0);
    if (!v) return "-";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let x = v;
    while (x >= 1024 && i < units.length - 1) {
      x /= 1024;
      i++;
    }
    return `${x.toFixed(x >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  // §9.1 / B-7 — KST 변환 후 오늘이면 HH:mm:ss, 이전이면 YYYY-MM-DD HH:mm.
  function fmtDateShort(iso) {
    if (!iso) return "-";
    let s = String(iso).trim().replace(" ", "T");
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(s)) s += "Z";
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return String(iso);
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Seoul",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).formatToParts(d);
    const get = (t) => (parts.find((p) => p.type === t) || {}).value || "";
    const yy = get("year"), mm = get("month"), dd = get("day");
    const hh = get("hour"), mi = get("minute"), ss = get("second");
    // 현재 KST 오늘 날짜와 비교
    const now = new Date();
    const nowParts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Seoul",
      year: "numeric", month: "2-digit", day: "2-digit",
    }).formatToParts(now);
    const ny = (nowParts.find((p) => p.type === "year") || {}).value;
    const nm = (nowParts.find((p) => p.type === "month") || {}).value;
    const nd = (nowParts.find((p) => p.type === "day") || {}).value;
    if (yy === ny && mm === nm && dd === nd) {
      return `${hh}:${mi}:${ss}`;
    }
    return `${yy}-${mm}-${dd} ${hh}:${mi}`;
  }

  function fmtKst(iso) {
    if (!iso) return "-";
    let s = String(iso).trim().replace(" ", "T");
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(s)) s += "Z";
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return String(iso);
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Seoul",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).formatToParts(d);
    const get = (t) => (parts.find((p) => p.type === t) || {}).value || "";
    return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}:${get("second")}`;
  }

  // P9 §6.1 — local_path 의 부모 폴더(= 책 폴더). 서버는 Windows 경로(L:\...) 로
  // 주므로 `\` 와 `/` 둘 다 잘라야 한다.
  function parentDir(p) {
    const s = String(p || "");
    const i = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
    return i > 0 ? s.slice(0, i) : "";
  }

  // P9 §6.2 — `scan` 행 한 줄의 문구와 상태 색. new_books 는 훅 미도착이면 null —
  // 에러가 아니라 정상 경로다(§12-B). null/0 이면 `· 새 도서 N권` 뒷부분만 생략.
  function scanLineText(scan) {
    if (!scan) return null;
    const status = scan.status || "";
    const fmt = (iso) => fmtKst(iso);
    if (status === "pending") {
      const at = scan.queued_at ? fmtDateShort(scan.queued_at) : "-";
      return { text: `⏳ 스캔 대기 · ${at} 이후 조용하면 시작`, cls: "gdrs-scan-pending", title: scan.queued_at ? fmt(scan.queued_at) : "" };
    }
    if (status === "running") {
      const at = scan.started_at ? fmtDateShort(scan.started_at) : "-";
      return { text: `🔄 스캔 중… ${at} 시작`, cls: "gdrs-scan-running", title: scan.started_at ? fmt(scan.started_at) : "" };
    }
    if (status === "done") {
      const at = scan.finished_at ? fmtDateShort(scan.finished_at) : "-";
      let tail = "";
      if (scan.new_books) tail = ` · 새 도서 ${scan.new_books}권`;
      return { text: `✅ 스캔 완료 ${at}${tail}`, cls: "gdrs-scan-done", title: scan.finished_at ? fmt(scan.finished_at) : "" };
    }
    if (status === "failed") {
      const err = String(scan.error || "").slice(0, 120);
      return { text: `❌ 스캔 실패 · ${err}`, cls: "gdrs-scan-failed", title: err };
    }
    if (status === "skipped") {
      const reason = scan.reason === "library_root" ? "라이브러리 루트"
        : scan.reason === "no_library" ? "보관함 없음" : scan.reason || "";
      return { text: `⏭ 스캔 안 함 · ${reason}`, cls: "gdrs-scan-skipped", title: reason };
    }
    return null;
  }

  function renderScanLine(pathTd, j) {
    const scan = state.scans[parentDir(j.local_path)];
    const line = scanLineText(scan);
    if (!line) return;   // 스캔 정보가 없으면 아무 줄도 붙이지 않는다 (§6.1)
    const div = document.createElement("div");
    div.className = `gdrs-scan-line ${line.cls}`;
    div.textContent = line.text;
    if (line.title) div.title = line.title;
    pathTd.appendChild(div);
  }

  function renderStatus(payload) {
    if (!payload || !payload.success) {
      $("gdrs-status-val").textContent = "error";
      $("gdrs-error-row").hidden = false;
      $("gdrs-error-text").textContent = (payload && payload.error) || "응답 실패";
      return;
    }
    // 제목에 설치 버전 표기 (한 번만 붙인다)
    const titleEl = document.querySelector(".gdrs-title-group h2");
    if (titleEl && payload.plugin_version && !titleEl.dataset.ver) {
      titleEl.dataset.ver = payload.plugin_version;
      titleEl.appendChild(
        Object.assign(document.createElement("span"), {
          className: "gdrs-title-ver",
          textContent: ` v${payload.plugin_version}`,
        })
      );
    }
    $("gdrs-status-val").textContent = payload.status || "-";
    $("gdrs-last-poll").textContent = fmtKst(payload.last_poll_at);
    const tok = payload.page_token || "";
    $("gdrs-token").textContent = tok || "(없음)";
    if (payload.error) {
      $("gdrs-error-row").hidden = false;
      $("gdrs-error-text").textContent = payload.error;
    } else {
      $("gdrs-error-row").hidden = true;
      $("gdrs-error-text").textContent = "";
    }
  }

  function renderPreflight(p) {
    const cell = $("gdrs-preflight");
    if (!p) {
      cell.textContent = "미확인";
      cell.classList.remove("gdrs-preflight-ok", "gdrs-preflight-fail");
      return;
    }
    const probe = (p.probe && p.probe.ok) ? "OK" : "FAIL";
    const line = `${p.rclone_version || "?"} · ${p.transfer_remote || "?"} · probe ${probe}`;
    cell.textContent = line;
    cell.classList.remove("gdrs-preflight-ok", "gdrs-preflight-fail");
    if (p.success) cell.classList.add("gdrs-preflight-ok");
    else cell.classList.add("gdrs-preflight-fail");
  }

  function cell(text, klass, title) {
    const td = document.createElement("td");
    if (klass) td.className = klass;
    td.textContent = text == null ? "" : String(text);
    if (title) td.title = title;
    return td;
  }

  function rowErrorSpan(text) {
    const div = document.createElement("div");
    div.className = "gdrs-row-error";
    div.textContent = text;
    return div;
  }

  function resultBadgeSpan(value) {
    const v = value || "";
    const label = RESULT_LABEL[v] || v || "-";
    const span = document.createElement("span");
    span.className = `gdrs-result-badge gdrs-result-${v || "unknown"}`;
    span.textContent = label;
    return span;
  }

  function renderJobs(jobs) {
    const body = $("gdrs-jobs-body");
    body.innerHTML = "";

    if (!jobs || !jobs.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 10;
      td.className = "gdrs-empty";
      td.textContent = state.total
        ? "현재 페이지에 조건에 맞는 변경 이벤트가 없습니다."
        : "감지된 변경 없음";
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }

    for (const j of jobs) {
      const tr = document.createElement("tr");
      // 체크박스
      const checkTd = document.createElement("td");
      checkTd.className = "gdrs-col-check";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = state.selected.has(j.id);
      cb.dataset.jobId = j.id;
      cb.addEventListener("change", () => {
        if (cb.checked) state.selected.add(j.id);
        else state.selected.delete(j.id);
        $("gdrs-retry-selected").disabled = state.selected.size === 0;
      });
      checkTd.appendChild(cb);
      tr.appendChild(checkTd);

      tr.appendChild(cell(j.id, "gdrs-id-cell"));

      const actionTd = document.createElement("td");
      const actionLbl = ACTION_LABEL[j.action] || String(j.action || "-").toUpperCase();
      const actionBadge = document.createElement("span");
      actionBadge.className = `gdrs-action-badge gdrs-action-${j.action}`;
      actionBadge.textContent = actionLbl;
      actionTd.appendChild(actionBadge);
      // B-8 — attempts > 1 일 때만 표시
      if (Number(j.attempts) > 1) {
        const attSpan = document.createElement("span");
        attSpan.className = "gdrs-attempts-over";
        attSpan.textContent = ` · ${j.attempts}회`;
        actionTd.appendChild(attSpan);
      }
      tr.appendChild(actionTd);

      const statusTd = document.createElement("td");
      const statusBadge = document.createElement("span");
      statusBadge.className = `gdrs-status-badge gdrs-status-${j.status || "unknown"}`;
      statusBadge.textContent = STATUS_LABEL[j.status] || j.status || "-";
      statusTd.appendChild(statusBadge);
      tr.appendChild(statusTd);

      // §9.2 / B-3 — result 열 별도
      const resultTd = document.createElement("td");
      resultTd.className = "gdrs-result-cell";
      resultTd.appendChild(resultBadgeSpan(j.result));
      tr.appendChild(resultTd);

      // §9.2 / B-2 — local_path 는 title 로만. 한 컬럼은 remote_path 한 줄 ellipsis.
      const pathTd = document.createElement("td");
      pathTd.className = "gdrs-path-cell";
      pathTd.title = (j.local_path || "") + (j.removed_path ? `\n(이전: ${j.removed_path})` : "");
      pathTd.textContent = j.remote_path || "";
      tr.appendChild(pathTd);

      // B-8 — size nowrap / line break 금지
      tr.appendChild(cell(fmtBytes(j.size), "gdrs-size-cell", String(j.size || 0)));
      // B-4 — bytes_done 별도
      tr.appendChild(cell(fmtBytes(j.bytes_done), "gdrs-bytes-cell", String(j.bytes_done || 0)));
      // B-7 — 날짜는 짧게
      tr.appendChild(cell(fmtDateShort(j.modified_time), "gdrs-date-cell", fmtKst(j.modified_time)));
      tr.appendChild(cell(fmtDateShort(j.created_at), "gdrs-date-cell", fmtKst(j.created_at)));

      // 행 단위 에러/이전 경로 한 줄 표시 (디테일) — ellipsis 안에 묻히지 않게
      if (j.removed_path || j.error) {
        // 한 행에 inline 표시
        if (j.removed_path) {
          const prev = document.createElement("div");
          prev.className = "gdrs-prev-path";
          prev.textContent = `이전 ${j.removed_path}`;
          pathTd.appendChild(prev);
        }
        if (j.error) pathTd.appendChild(rowErrorSpan(j.error));
      }

      // P9 §6.1 — 경로 칸에 스캔 상태 한 줄 (scan 정보가 없으면 아무 줄도 안 붙인다)
      renderScanLine(pathTd, j);

      body.appendChild(tr);
    }

    // 페이지 노트
    const start = (state.page - 1) * state.pageSize + 1;
    const end = (state.page - 1) * state.pageSize + jobs.length;
    $("gdrs-jobs-note").textContent =
      `표시 ${start}-${end} / 전체 ${state.total}건 · ${state.pages}페이지`;
  }

  const nfmt = (n) => Number(n || 0).toLocaleString("ko-KR");

  /**
   * 진행 상황 표시.
   *
   * 구 버전은 `전체 N건 · 완료 M · 바이트` 였는데 세 값이 모두 오해를 불렀다.
   * `전체` 는 감지 이력 총계라 할 일처럼 보이고, `완료` 는 status='completed' 만
   * 세서 실제로 중복 판정된 수십만 건이 진행에 반영되지 않았다. 남은 건수는
   * 아예 없어서 화면만 보고는 얼마나 남았는지 알 수 없었다.
   *
   * 이제 종결(완료+중복+실패) / 전체 로 진행률을 내고, 남은 건수와 복사·중복·실패를
   * 각각 따로 보여준다.
   */
  function renderProgress(summary, statusCounts) {
    const pctEl = $("gdrs-progress-pct");
    const sumEl = $("gdrs-progress-summary");
    const fillEl = $("gdrs-progress-fill");
    const trackEl = $("gdrs-progress-track");
    const detEl = $("gdrs-progress-detail");
    if (!summary) {
      if (sumEl) sumEl.textContent = "";
      if (pctEl) pctEl.textContent = "–";
      if (fillEl) fillEl.style.width = "0%";
      if (detEl) detEl.textContent = "";
      return;
    }

    const counts = statusCounts || {};
    const at = (k) => Number(counts[k] || 0);
    const { total, done, left, pct } = computeProgress(summary, statusCounts);

    if (pctEl) pctEl.textContent = `${pct.toFixed(1)}%`;
    if (sumEl) sumEl.textContent = `${nfmt(done)} / ${nfmt(total)} 처리 · 남음 ${nfmt(left)}`;
    if (fillEl) fillEl.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    if (trackEl) trackEl.setAttribute("aria-valuenow", pct.toFixed(1));

    if (detEl) {
      detEl.replaceChildren();
      const chip = (label, value, cls) => {
        if (!value) return;
        const el = document.createElement("span");
        el.className = `gdrs-progress-chip${cls ? " " + cls : ""}`;
        el.textContent = `${label} ${nfmt(value)}`;
        detEl.appendChild(el);
      };
      chip("복사", at("completed") || Number(summary.completed || 0), "is-copied");
      const bytes = fmtBytes(summary.bytes_done);
      if (bytes && bytes !== "-") {
        const el = document.createElement("span");
        el.className = "gdrs-progress-chip is-bytes";
        el.textContent = bytes;
        detEl.appendChild(el);
      }
      chip("중복", at("skipped"), "is-dup");
      chip("처리 중", at("processing"), "is-run");
      chip("재시도", at("retry"), "is-retry");
      chip("실패", at("failed"), "is-failed");
    }
  }

  function renderPagination() {
    const wrap = $("gdrs-pagination");
    wrap.innerHTML = "";
    if (!state.pages || state.pages <= 1) return;
    const cur = state.page;
    const pages = state.pages;
    const range = [];
    const add = (p) => {
      if (p >= 1 && p <= pages && !range.find((x) => x.n === p)) {
        range.push({ n: p, kind: "n" });
      }
    };
    range.push({ n: 1, kind: "first" });
    if (cur > 1) range.push({ n: cur - 1, kind: "prev" });
    for (let p = Math.max(1, cur - 2); p <= Math.min(pages, cur + 2); p++) add(p);
    if (cur < pages) range.push({ n: cur + 1, kind: "next" });
    range.push({ n: pages, kind: "last" });

    const seen = new Set();
    for (const r of range) {
      if (seen.has(r.n)) continue;
      seen.add(r.n);
      const btn = document.createElement("button");
      btn.textContent = r.n === 1 ? "«"
        : r.n === pages ? "»"
        : r.n === cur - 1 ? "‹"
        : r.n === cur + 1 ? "›"
        : String(r.n);
      if (r.n === cur) btn.classList.add("active");
      if ((r.kind === "prev" && cur === 1) ||
          (r.kind === "next" && cur === pages)) {
        btn.disabled = true;
      } else {
        btn.addEventListener("click", () => {
          if (r.n === state.page) return;
          loadJobs(r.n);
        });
      }
      wrap.appendChild(btn);
    }
  }

  function readStateFromUi() {
    state.pageSize = parseInt($("gdrs-page-size").value, 10) || 100;
    state.status = $("gdrs-filter-status").value || "";
    state.action = $("gdrs-filter-action").value || "";
    state.result = $("gdrs-filter-result").value || "";
    state.search = $("gdrs-filter-search").value.trim();
    state.order = $("gdrs-filter-order").value || "desc";
  }

  // §9 — 검색 버튼 또는 Enter → page=1 으로 실행. reset → 기본값 + page=1.
  function applySearch() {
    readStateFromUi();
    state.page = 1;
    state.selected.clear();
    $("gdrs-retry-selected").disabled = true;
    refresh({ loadJobsOnly: true, forceJobs: true });
  }

  function applyReset() {
    $("gdrs-filter-status").value = "";
    $("gdrs-filter-action").value = "";
    $("gdrs-filter-result").value = "";
    $("gdrs-filter-search").value = "";
    $("gdrs-page-size").value = "100";
    $("gdrs-filter-order").value = "desc";
    state.selected.clear();
    $("gdrs-retry-selected").disabled = true;
    applySearch();
  }

  async function loadJobs(page) {
    if (page) state.page = page;
    readStateFromUi();
    const params = new URLSearchParams();
    params.set("page", String(state.page));
    params.set("page_size", String(state.pageSize));
    if (state.status) params.set("status", state.status);
    if (state.action) params.set("action", state.action);
    if (state.result) params.set("result", state.result);
    if (state.search) params.set("search", state.search);
    if (state.order) params.set("order", state.order);
    let resp;
    try {
      resp = await fetch(`${API_JOBS}?${params.toString()}`, { credentials: "same-origin" });
    } catch (exc) {
      $("gdrs-jobs-body").innerHTML =
        `<tr><td colspan="10" class="gdrs-empty">/jobs 호출 실패: ${exc}</td></tr>`;
      return;
    }
    if (!resp.ok) {
      $("gdrs-jobs-body").innerHTML =
        `<tr><td colspan="10" class="gdrs-empty">/jobs HTTP ${resp.status}</td></tr>`;
      return;
    }
    const data = await resp.json();
    state.total = Number(data.total || 0);
    state.page = Number(data.page || state.page);
    state.pageSize = Number(data.page_size || state.pageSize);
    state.pages = Number(data.pages || 0);
    state.scans = data.scans || {};   // P9
    const items = (data.items || data.jobs || []);
    renderJobs(items);
    renderPagination();
    renderProgress(data.summary || null, data.status_counts || null);
    // worker_status 도 표시 가능하면 summary 카드에 노출 (data.worker_status)
    if (data.worker_status) {
      const sv = $("gdrs-status-val");
      sv.textContent = data.worker_status.status || "-";
      $("gdrs-last-poll").textContent = fmtKst(data.worker_status.last_poll_at);
      if (data.worker_status.error) {
        $("gdrs-error-row").hidden = false;
        $("gdrs-error-text").textContent = data.worker_status.error;
      }
    }
    // 체크박스 동기화
    const all = $("gdrs-select-all");
    if (all) all.checked = false;
  }

  async function postRetry(body) {
    let resp;
    try {
      resp = await fetch(API_RETRY, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (exc) {
      alert(`retry 호출 실패: ${exc}`);
      return null;
    }
    const data = await resp.json().catch(() => ({ success: false, error: `HTTP ${resp.status}` }));
    if (!resp.ok && resp.status !== 409) {
      alert(`retry 실패: ${data.error || resp.status}`);
    } else if (resp.status === 409) {
      alert(`재시도 불가: rejected=${(data.rejected_ids||[]).join(",")} missing=${(data.missing_ids||[]).join(",")}`);
    }
    return data;
  }

  async function postCleanup(body) {
    let resp;
    try {
      resp = await fetch(API_CLEANUP, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (exc) {
      alert(`cleanup 호출 실패: ${exc}`);
      return null;
    }
    const data = await resp.json().catch(() => ({ success: false, error: `HTTP ${resp.status}` }));
    if (!resp.ok) {
      alert(`cleanup 실패: ${data.error || resp.status}`);
    } else {
      alert(`삭제 ${data.deleted}건 (delete_all=${data.delete_all})`);
    }
    return data;
  }

  async function refresh(opts = {}) {
    try {
      const [st, jb] = await Promise.all([
        fetch(API_STATUS, { credentials: "same-origin" }).then((r) => r.json()),
        (async () => {
          readStateFromUi();
          const params = new URLSearchParams();
          params.set("page", String(state.page));
          params.set("page_size", String(state.pageSize));
          if (state.status) params.set("status", state.status);
          if (state.action) params.set("action", state.action);
          if (state.result) params.set("result", state.result);
          if (state.search) params.set("search", state.search);
          if (state.order) params.set("order", state.order);
          const r = await fetch(`${API_JOBS}?${params.toString()}`, { credentials: "same-origin" });
          return r.json();
        })(),
      ]);
      renderStatus(st);
      state.total = Number(jb.total || 0);
      state.page = Number(jb.page || state.page);
      state.pageSize = Number(jb.page_size || state.pageSize);
      state.pages = Number(jb.pages || 0);
      state.scans = jb.scans || {};   // P9
      renderJobs(jb.items || jb.jobs || []);
      renderPagination();
      renderProgress(jb.summary || null, jb.status_counts || null);
    } catch (exc) {
      $("gdrs-status-val").textContent = "error";
      $("gdrs-error-row").hidden = false;
      $("gdrs-error-text").textContent = String(exc);
    }
    try {
      const pf = await fetch(API_PREFLIGHT, { credentials: "same-origin" });
      if (pf && pf.ok) {
        const p = await pf.json();
        renderPreflight(p);
      }
    } catch (exc) {
      renderPreflight(null);
    }
  }

  function boot() {
    $("gdrs-search-btn").addEventListener("click", applySearch);
    $("gdrs-reset-btn").addEventListener("click", applyReset);
    $("gdrs-filter-search").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") applySearch();
    });
    $("gdrs-retry-selected").addEventListener("click", async () => {
      const ids = Array.from(state.selected);
      if (!ids.length) return;
      if (!window.confirm(`선택한 ${ids.length}건을 재시도합니다. 계속할까요?`)) return;
      const out = await postRetry({ job_ids: ids });
      if (out && out.success) {
        state.selected.clear();
        $("gdrs-retry-selected").disabled = true;
        loadJobs(1);
      }
    });
    $("gdrs-retry-failed-all").addEventListener("click", async () => {
      if (!window.confirm("실패한 모든 job 을 재시도합니다. 계속할까요?")) return;
      const out = await postRetry({ failed_all: true });
      if (out && out.success) {
        state.selected.clear();
        $("gdrs-retry-selected").disabled = true;
        loadJobs(1);
      }
    });
    $("gdrs-cleanup-retention").addEventListener("click", async () => {
      if (!window.confirm("보존기간 외 종결 이력을 삭제합니다. 되돌릴 수 없습니다. 계속할까요?")) return;
      const out = await postCleanup({ delete_all: false });
      if (out && out.success) loadJobs(state.page);
    });
    $("gdrs-cleanup-all").addEventListener("click", async () => {
      if (!window.confirm("종결된 모든 이력을 전체 삭제합니다. 되돌릴 수 없습니다. 계속할까요?")) return;
      const out = await postCleanup({ delete_all: true });
      if (out && out.success) loadJobs(state.page);
    });
    const selectAll = $("gdrs-select-all");
    if (selectAll) {
      selectAll.addEventListener("change", () => {
        const rows = document.querySelectorAll("#gdrs-jobs-body tr");
        rows.forEach((tr) => {
          const cb = tr.querySelector('input[type="checkbox"]');
          if (cb) {
            cb.checked = selectAll.checked;
            const id = Number(cb.dataset.jobId);
            if (selectAll.checked) state.selected.add(id);
            else state.selected.delete(id);
          }
        });
        $("gdrs-retry-selected").disabled = !selectAll.checked;
      });
    }
    refresh();
    setInterval(refresh, 15000);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
