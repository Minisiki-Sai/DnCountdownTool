/**
 * 人肉核对用：把 fixtures 里每个包按真实规则回放一遍，打印语音与倒计时。
 *   node --experimental-strip-types frontend/tests/helpers/printReplay.ts
 */

import { readdirSync } from "node:fs";
import { join } from "node:path";
import { describeOutcome, loadFixture, replayFixture } from "./captureReplayHarness.ts";

const dir = join(import.meta.dirname, "..", "..", "..", "tests", "fixtures", "capture_replay");
for (const file of readdirSync(dir).filter((name) => name.endsWith(".json")).sort()) {
  const outcome = replayFixture(loadFixture(join(dir, file)));
  console.log(describeOutcome(outcome));
  console.log("  倒计时变化 " + outcome.countdowns.length + " 次");
  console.log();
}
