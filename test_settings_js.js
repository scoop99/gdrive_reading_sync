// P11 — settings.js 의 순수 함수 `pickRemoteOptions` 회귀 테스트.
// 실행: node test_settings_js.js
// settings.js 는 new Function 스코프에서 config 를 인자로 받으므로, require 로
// 뜰 때 최상단의 DOM 코드를 건너뛰도록 최상단 return 가드가 있다 (§2.1).
const assert = require("assert");
const { pickRemoteOptions, resolvePickedPath } = require("./settings.js");

// T-P11-1: 저장값이 목록에 있다 → 그대로 선택, missing 없음
let r = pickRemoteOptions(["a", "b"], "b");
assert.strictEqual(r.selected, "b", r.selected);
assert.strictEqual(r.options.length, 2, JSON.stringify(r));
assert.ok(r.options.every((o) => !o.missing), JSON.stringify(r.options));

// T-P11-2: 저장값이 목록에 없어도 무단 교체 금지 → 저장값 유지, missing:true 맨 앞
r = pickRemoteOptions(["a", "b"], "zzz");
assert.strictEqual(r.selected, "zzz", "저장값을 다른 값으로 갈아치우면 안 된다");
assert.strictEqual(r.options.length, 3, JSON.stringify(r));
const z = r.options[0];
assert.strictEqual(z.value, "zzz", JSON.stringify(r.options));
assert.strictEqual(z.missing, true, JSON.stringify(r.options));

// T-P11-3: 조회 실패([])여도 저장값 보존 → 값이 산다 (핵심)
r = pickRemoteOptions([], "gdrive");
assert.strictEqual(r.selected, "gdrive", r.selected);
assert.strictEqual(r.options.length, 1, JSON.stringify(r));
assert.strictEqual(r.options[0].missing, true, JSON.stringify(r.options));

// T-P11-4: 저장값이 없으면 목록 첫 항목
r = pickRemoteOptions(["a", "b"], "");
assert.strictEqual(r.selected, "a", r.selected);

// T-P11-5: 둘 다 비면 빈 결과
r = pickRemoteOptions([], "");
assert.strictEqual(r.selected, "", r.selected);
assert.strictEqual(r.options.length, 0, JSON.stringify(r));

// T-P11-6: 조회 결과가 null 이면 뻑나지 않는다, 저장값 유지
r = pickRemoteOptions(null, "x");
assert.strictEqual(r.selected, "x", r.selected);
assert.strictEqual(r.options.length, 1, JSON.stringify(r));

// T-P11-7: 저장값 공백 trim
r = pickRemoteOptions(["a"], "  a  ");
assert.strictEqual(r.selected, "a", r.selected);
assert.strictEqual(r.options.length, 1, JSON.stringify(r));
assert.ok(!r.options[0].missing, JSON.stringify(r.options));

console.log("  OK pickRemoteOptions 7-case (값 보존 · 무단 교체 금지 · 조회 실패 생존)");

// ---------------------------------------------------------------------------
// P12 — resolvePickedPath 9-case. 본체 탐색 API 가 디렉터리만 주므로 파일칸은
// 고른 폴더에 고정 파일명을 붙여 완성한다. 구분자는 끝에서 한 번만.
// ---------------------------------------------------------------------------

// T-P12-1: 폴더칸은 고른 폴더 그대로
assert.strictEqual(resolvePickedPath("LOCAL_ROOT", "D:\\READING", true), "D:\\READING");

// T-P12-2: 파일칸(Windows) — rclone.exe 가 붙는다
assert.strictEqual(resolvePickedPath("RCLONE_BIN", "D:\\rclone", true), "D:\\rclone\\rclone.exe");

// T-P12-3: 끝 구분자가 있어도 중복 없음
assert.strictEqual(resolvePickedPath("RCLONE_BIN", "D:\\rclone\\", true), "D:\\rclone\\rclone.exe");

// T-P12-4: 파일칸(POSIX) — rclone.conf
assert.strictEqual(resolvePickedPath("RCLONE_CONFIG", "/etc/rclone", false), "/etc/rclone/rclone.conf");

// T-P12-5: 리눅스는 확장자 없음
assert.strictEqual(resolvePickedPath("RCLONE_BIN", "/usr/bin", false), "/usr/bin/rclone");

// T-P12-6: 폴더칸 끝 구분자 제거
assert.strictEqual(resolvePickedPath("LOG_DIR", "/var/log/", false), "/var/log");

// T-P12-7: 빈 입력에 구분자만 남지 않는다
assert.strictEqual(resolvePickedPath("LOCAL_ROOT", "", true), "");

// T-P12-8: null 이어도 던지지 않는다
assert.strictEqual(resolvePickedPath("LOCAL_ROOT", null, true), "");

// T-P12-9: 모르는 키는 폴더 그대로
assert.strictEqual(resolvePickedPath("UNKNOWN_KEY", "/x", false), "/x");

console.log("  OK resolvePickedPath 9-case (파일명 자동 결합 · 끝 구분자 중복 없음 · 리눅스 확장자 없음)");