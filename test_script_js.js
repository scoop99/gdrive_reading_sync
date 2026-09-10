// script.js 의 순수 함수 회귀. 프레임워크 없음 — node test_script_js.js
//
// 구 화면이 `전체 N건 · 완료 M` 만 보여줘서 실제 84% 진행을 1% 처럼 보이게 했다.
// 여기서 지키는 계약은 하나다: **진행률의 분자는 종결(완료+중복+실패) 전부다.**
const assert = require("assert");
const { computeProgress } = require("./script.js");

// 1) 운영 실측값 (2026-09-10). 이게 이 수정의 이유다.
{
  const summary = { all: 404410, completed: 4055, bytes_done: 37151637504 };
  const counts = {
    skipped: 335931, queued: 64094, completed: 4055, processing: 266, failed: 64,
  };
  const r = computeProgress(summary, counts);
  assert.strictEqual(r.total, 404410);
  assert.strictEqual(r.done, 335931 + 4055 + 64);      // 340050
  assert.strictEqual(r.left, 64094 + 266);             // 64360
  assert.ok(Math.abs(r.pct - 84.08) < 0.05, `pct=${r.pct}`);
  // 구 화면은 completed 만 세서 1.0% 로 보였다. 그게 버그였다.
  assert.ok(r.pct > 80, "종결 전부를 분자로 세야 한다");
}

// 2) 중복(skipped)이 진행에 반드시 포함된다
{
  const r = computeProgress({ all: 100 }, { skipped: 90, queued: 10 });
  assert.strictEqual(r.done, 90);
  assert.strictEqual(r.left, 10);
  assert.strictEqual(r.pct, 90);
}

// 3) 실패도 종결이다 — 영원히 안 끝나는 것처럼 보이면 안 된다
{
  const r = computeProgress({ all: 10 }, { failed: 10 });
  assert.strictEqual(r.done, 10);
  assert.strictEqual(r.left, 0);
  assert.strictEqual(r.pct, 100);
}

// 4) status_counts 가 없는 구 응답 — completed 로라도 계산하고 던지지 않는다
{
  const r = computeProgress({ all: 200, completed: 50 }, null);
  assert.strictEqual(r.done, 50);
  assert.strictEqual(r.left, 150);
  assert.strictEqual(r.pct, 25);
}

// 5) 빈 입력 — 0 나누기로 죽지 않는다
{
  assert.deepStrictEqual(computeProgress(null, null), { total: 0, done: 0, left: 0, pct: 0 });
  assert.deepStrictEqual(computeProgress({}, {}), { total: 0, done: 0, left: 0, pct: 0 });
}

// 6) retry / dry_run 도 '남음' 이다
{
  const r = computeProgress({ all: 10 }, { completed: 4, retry: 3, dry_run: 3 });
  assert.strictEqual(r.done, 4);
  assert.strictEqual(r.left, 6);
}

// 7) 모르는 상태가 섞여도 던지지 않는다 (앞으로 상태가 늘어날 수 있다)
{
  const r = computeProgress({ all: 5 }, { completed: 2, some_new_state: 3 });
  assert.strictEqual(r.done, 2);
  assert.strictEqual(r.left, 0);   // 분류 못 하는 건 '남음' 에 안 넣는다
}

console.log("  OK computeProgress 7-case (종결=완료+중복+실패 · 구응답 폴백 · 빈입력 안전)");
