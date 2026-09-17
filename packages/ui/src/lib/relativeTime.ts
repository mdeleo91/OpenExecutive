/**
 * Shared "N units ago" formatter — extracted from RecentSessions/
 * DynamicSection/jobs/evals where the same function had been copy-pasted.
 *
 * Renders an ISO timestamp as the most-significant non-zero unit:
 *   - "just now"   if  < 1 minute
 *   - "{m}m ago"   if  < 1 hour
 *   - "{h}h ago"   if  < 1 day
 *   - "{d}d ago"   otherwise
 *
 * Empty input returns "" so callers can fall through to a "Never …" label
 * without sprinkling guards everywhere. Invalid input falls back to ""
 * for the same reason; we never throw from a display helper.
 */
/**
 * Parse an API timestamp to epoch ms, treating a timestamp with no timezone
 * as UTC.
 *
 * The chat-history rows are UTC either way, but the Postgres backend returns
 * an explicit `+00:00` offset while older SQLite rows are naive (written from
 * `utcnow()`). `new Date("2026-01-01T12:00:00")` parses a naive value as
 * *local* time, so west of UTC every such row looked hours in the past (and
 * east of it, in the future — "just now" for a day-old chat). Returns NaN for
 * empty or unparseable input; callers already handle that.
 */
export function parseApiTimestamp(iso: string): number {
  if (!iso) return Number.NaN;
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso.trim());
  return new Date(hasZone ? iso : `${iso}Z`).getTime();
}

export function formatRelativeTime(iso: string): string {
  if (!iso) return "";
  const ts = parseApiTimestamp(iso);
  if (!Number.isFinite(ts)) return "";
  const diff = Date.now() - ts;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}
