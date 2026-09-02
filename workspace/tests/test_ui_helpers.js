"use strict";
/**
 * Unit tests for pure UI helpers (node:test).
 * Run: node --test workspace/tests/test_ui_helpers.js
 * Regression guards for P0-1 (prompt data loss), P1-1 (backoff), P1-2 (bounds).
 */
const test = require("node:test");
const assert = require("node:assert");
const U = require("../static/ui_utils.js");

test("composerDecision: empty input never sends and never clears", () => {
  for (const text of ["", "   ", "\n"]) {
    const d = U.composerDecision(text, true, false, false);
    assert.equal(d.action, "block-empty");
    assert.equal(d.clearInput, false);
  }
});

test("composerDecision: disconnected socket keeps the prompt (P0-1)", () => {
  const d = U.composerDecision("important task", false, false, false);
  assert.equal(d.action, "block-offline");
  assert.equal(d.clearInput, false, "input must be preserved");
});

test("composerDecision: running agent blocks duplicate runs", () => {
  const d = U.composerDecision("task", true, true, false);
  assert.equal(d.action, "block-running");
  assert.equal(d.clearInput, false);
});

test("composerDecision: pending echo blocks double-submit race", () => {
  const d = U.composerDecision("task", true, false, true);
  assert.equal(d.action, "block-pending");
  assert.equal(d.clearInput, false);
});

test("composerDecision: healthy path sends and clears", () => {
  const d = U.composerDecision("  task  ", true, false, false);
  assert.equal(d.action, "send");
  assert.equal(d.clearInput, true);
});

test("backoffDelay: exponential, capped at 15s, never zero", () => {
  assert.equal(U.backoffDelay(1), 1000);
  assert.equal(U.backoffDelay(2), 2000);
  assert.equal(U.backoffDelay(3), 4000);
  assert.equal(U.backoffDelay(5), 15000, "capped");
  assert.equal(U.backoffDelay(50), 15000, "hard cap holds");
  assert.equal(U.backoffDelay(0), 1000, "invalid attempt clamps to first");
  assert.ok(U.backoffDelay(1) > 0);
});

test("pruneCount: within bound prunes nothing; overflow prunes the difference", () => {
  const kids = (n) => ({ children: Array(n) });
  assert.equal(U.pruneCount(kids(10), 400), 0);
  assert.equal(U.pruneCount(kids(401), 400), 1);
  assert.equal(U.pruneCount(kids(1000), 400), 600);
  assert.equal(U.pruneCount(null, 400), 0, "null-safe");
});

test("absoluteTime: local + UTC rendering, bad input empty", () => {
  const out = U.absoluteTime(Date.UTC(2026, 8, 1, 19, 41, 7));
  assert.match(out, /UTC: 2026-09-01T19:41:07/);
  assert.ok(out.length > 10, "local part present");
  assert.equal(U.absoluteTime(NaN), "");
});
