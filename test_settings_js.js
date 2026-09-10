// P11 — settings.js 의 순수 함수 `pickRemoteOptions` 회귀 테스트.
// 실행: node test_settings_js.js
// settings.js 는 new Function 스코프에서 config 를 인자로 받으므로, require 로
// 뜰 때 최상단의 DOM 코드를 건너뛰도록 최상단 return 가드가 있다 (§2.1).
const assert = require("assert");
const { pickRemoteOptions } = require("./settings.js");

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