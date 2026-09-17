import assert from "node:assert/strict";
import test from "node:test";
import { formatRelativeTime, parseApiTimestamp } from "../src/lib/relativeTime.ts";

// The chat-history API returns UTC either way, but the Postgres backend marks
// the offset and older SQLite rows do not. Both must land on the same instant,
// or the sidebar's "N ago" is wrong by the viewer's UTC offset.
test("a naive timestamp is parsed as UTC, not local time", () => {
  assert.equal(
    parseApiTimestamp("2026-01-02T03:04:05"),
    Date.UTC(2026, 0, 2, 3, 4, 5),
  );
});

test("naive and offset-marked forms of the same instant agree", () => {
  const naive = parseApiTimestamp("2026-01-02T03:04:05.123");
  assert.equal(naive, parseApiTimestamp("2026-01-02T03:04:05.123+00:00"));
  assert.equal(naive, parseApiTimestamp("2026-01-02T03:04:05.123Z"));
});

test("a non-UTC offset is respected, not overwritten", () => {
  assert.equal(
    parseApiTimestamp("2026-01-02T03:04:05-05:00"),
    Date.UTC(2026, 0, 2, 8, 4, 5),
  );
});

test("empty and unparseable input yield NaN", () => {
  assert.ok(Number.isNaN(parseApiTimestamp("")));
  assert.ok(Number.isNaN(parseApiTimestamp("not a date")));
});

test("formatRelativeTime reads a naive timestamp as UTC", () => {
  const twoHoursAgo = new Date(Date.now() - 2 * 3600_000)
    .toISOString()
    .replace("Z", "");
  assert.equal(formatRelativeTime(twoHoursAgo), "2h ago");
});

test("formatRelativeTime still returns empty string for bad input", () => {
  assert.equal(formatRelativeTime(""), "");
  assert.equal(formatRelativeTime("nope"), "");
});
