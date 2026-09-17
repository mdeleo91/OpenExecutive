"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Icon from "@/components/Icon";
import type { SessionSummary } from "@/lib/api";
import { groupSessionsByDate, type GroupKey } from "@/lib/sessionGroups";
import { formatRelativeTime } from "@/lib/relativeTime";

const GROUP_CAP = 8;
const DEFAULT_COLLAPSED = new Set<GroupKey>(["prev30", "older"]);
// Mirrors SESSION_TITLE_MAX_LEN on the API so the input can't submit a
// title the server would reject with a 422.
const TITLE_MAX_LEN = 200;
// Blur commits the rename, but Escape (cancel) also blurs the input. Defer
// the commit one tick past the keydown so a cancel wins over the blur.
const BLUR_COMMIT_DELAY_MS = 120;

interface RecentSessionsProps {
  sessions: SessionSummary[];
  activeSessionId?: string;
  // True until the first /sessions fetch resolves — renders a skeleton
  // instead of flashing the empty state on every page load.
  isLoading?: boolean;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
  // Resolves when the server has the new title; rejects to leave the
  // editor open with the old draft so the user can retry.
  onRename: (sessionId: string, title: string) => Promise<void>;
}

// Row actions are hover-revealed on pointer devices. On touch there is no
// hover, so `pointer-coarse:` keeps them visible — otherwise rename/delete
// would be unreachable on a phone.
const ROW_ACTION_CLASS =
  "min-h-touch min-w-touch flex items-center justify-center rounded opacity-0 " +
  "group-hover:opacity-100 focus:opacity-100 pointer-coarse:opacity-100 " +
  "text-fg-muted hover:bg-surface-input/60 transition-opacity cursor-pointer";

export default function RecentSessions({
  sessions,
  activeSessionId,
  isLoading = false,
  onSelect,
  onDelete,
  onRename,
}: RecentSessionsProps) {
  const [query, setQuery] = useState("");
  const [collapsedOverride, setCollapsedOverride] = useState<Record<string, boolean>>({});
  const [showAll, setShowAll] = useState<Set<string>>(new Set());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [savingId, setSavingId] = useState<string | null>(null);
  const [renameError, setRenameError] = useState<string | null>(null);
  const editInputRef = useRef<HTMLInputElement>(null);
  // The rename editor is single-slot, but its blur commit is deferred — so
  // by the time the timer runs, the user may have opened a different row or
  // pressed Escape. These refs give the timer the *current* state (a closure
  // would see the values captured at blur time) and let a new editor cancel
  // a pending commit for the previous one.
  const blurTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const editingIdRef = useRef<string | null>(null);
  const savingIdRef = useRef<string | null>(null);
  const draftRef = useRef("");

  useEffect(() => {
    if (editingId) editInputRef.current?.select();
  }, [editingId]);

  // Never leave a commit queued after unmount.
  useEffect(() => clearBlurTimer, []);

  const trimmedQuery = query.trim().toLowerCase();
  const searching = trimmedQuery.length > 0;

  const filtered = useMemo(() => {
    if (!searching) return sessions;
    return sessions.filter((s) =>
      (s.title || "Untitled chat").toLowerCase().includes(trimmedQuery),
    );
  }, [sessions, searching, trimmedQuery]);

  const groups = useMemo(() => groupSessionsByDate(filtered), [filtered]);

  // Stored collapse state (default-collapsed unless the user toggled it). While
  // searching we force every group open so matches are never hidden, but the
  // stored state is preserved and re-applies once the query is cleared.
  const storedCollapsed = (key: GroupKey) =>
    key in collapsedOverride ? collapsedOverride[key] : DEFAULT_COLLAPSED.has(key);
  const isCollapsed = (key: GroupKey) => (searching ? false : storedCollapsed(key));
  const toggleCollapse = (key: GroupKey) =>
    setCollapsedOverride((prev) => ({ ...prev, [key]: !storedCollapsed(key) }));
  const revealAll = (key: GroupKey) =>
    setShowAll((prev) => new Set(prev).add(key));

  function clearBlurTimer() {
    if (blurTimerRef.current !== null) {
      clearTimeout(blurTimerRef.current);
      blurTimerRef.current = null;
    }
  }

  function startRename(s: SessionSummary) {
    // Opening another row cancels the previous row's pending blur commit.
    clearBlurTimer();
    setRenameError(null);
    setDraft(s.title || "");
    draftRef.current = s.title || "";
    editingIdRef.current = s.session_id;
    setEditingId(s.session_id);
  }

  /** Close the editor, but only if `sessionId` is still the row being edited. */
  function closeEditor(sessionId: string) {
    clearBlurTimer();
    if (editingIdRef.current !== sessionId) return;
    editingIdRef.current = null;
    setEditingId(null);
    setDraft("");
    draftRef.current = "";
    setRenameError(null);
  }

  async function commitRename(s: SessionSummary) {
    const title = draftRef.current.replace(/\s+/g, " ").trim();
    if (!title || title === s.title) {
      closeEditor(s.session_id);
      return;
    }
    savingIdRef.current = s.session_id;
    setSavingId(s.session_id);
    try {
      await onRename(s.session_id, title);
      closeEditor(s.session_id);
    } catch (err) {
      setRenameError(err instanceof Error ? err.message : "Rename failed");
    } finally {
      savingIdRef.current = null;
      setSavingId(null);
    }
  }

  return (
    <div className="flex flex-col min-h-0">
      {/* Pinned header — label + search, stays put while the list scrolls */}
      <div className="flex-shrink-0 px-2 pt-3 pb-2">
        <p className="px-2 pb-1.5 text-xs text-fg-subtle font-medium uppercase tracking-wide">
          Recent
        </p>
        {sessions.length > 0 && (
          <div className="relative">
            <Icon
              name="search"
              size="w-3.5 h-3.5"
              className="absolute left-2 top-1/2 -translate-y-1/2 text-fg-subtle pointer-events-none"
            />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search conversations"
              aria-label="Search conversations"
              className="w-full rounded-lg bg-surface-input/60 border border-line pl-7 pr-2 py-2 text-base sm:text-xs text-fg placeholder:text-fg-subtle focus:outline-none focus:border-fg-subtle"
            />
          </div>
        )}
      </div>

      {/* List — scrolls under the pinned header above. Below md it fills the
          height the drawer gives it; at md+ it is content-sized up to a cap
          so the sidebar footer stays put. */}
      <div className="flex-1 overflow-y-auto overscroll-y-contain min-h-0 md:flex-none md:max-h-[60dvh] px-2 pb-2">
        {isLoading ? (
          <div className="space-y-1.5 px-1 pt-1" role="status" aria-live="polite" aria-label="Loading conversations">
            {[0, 1, 2, 3].map((i) => (
              <div
                key={i}
                aria-hidden
                className="h-10 rounded-lg bg-surface-overlay/60 animate-pulse motion-reduce:animate-none"
              />
            ))}
          </div>
        ) : sessions.length === 0 ? (
          <p className="px-2 py-3 text-xs text-fg-subtle leading-relaxed">
            No conversations yet. Start a new chat and it will show up here — history
            is saved, so you can pick it up on any device.
          </p>
        ) : groups.length === 0 ? (
          <p className="px-2 py-3 text-xs text-fg-subtle">No conversations match.</p>
        ) : (
          groups.map((group) => {
            const collapsed = isCollapsed(group.key);
            const expandedAll = searching || showAll.has(group.key);
            const visible = expandedAll ? group.items : group.items.slice(0, GROUP_CAP);
            const hiddenCount = group.items.length - visible.length;
            return (
              <div key={group.key} className="mb-1">
                <button
                  type="button"
                  onClick={() => toggleCollapse(group.key)}
                  aria-expanded={!collapsed}
                  className="sticky top-0 z-10 w-full flex items-center gap-1.5 px-2 py-2 bg-surface-elevated text-fg-subtle hover:text-fg transition-colors cursor-pointer"
                >
                  <Icon
                    name="chevron-right"
                    size="w-3 h-3"
                    className={`transition-transform ${collapsed ? "" : "rotate-90"}`}
                  />
                  <span className="text-[11px] font-medium uppercase tracking-wide">
                    {group.label}
                  </span>
                  <span className="text-[10px] text-fg-subtle/70">{group.items.length}</span>
                </button>
                {!collapsed && (
                  <div className="space-y-0.5">
                    {visible.map((s) => {
                      const isEditing = editingId === s.session_id;
                      const isSaving = savingId === s.session_id;
                      return (
                        <div
                          key={s.session_id}
                          className={`group relative flex items-center rounded-lg transition-colors ${
                            activeSessionId === s.session_id
                              ? "bg-surface-overlay text-fg"
                              : "text-fg-muted hover:bg-surface-overlay/60 hover:text-fg"
                          }`}
                        >
                          {isEditing ? (
                            <form
                              className="flex-1 min-w-0 px-2 py-1.5"
                              onSubmit={(e) => {
                                e.preventDefault();
                                void commitRename(s);
                              }}
                            >
                              <input
                                ref={editInputRef}
                                value={draft}
                                maxLength={TITLE_MAX_LEN}
                                disabled={isSaving}
                                onChange={(e) => {
                                  setDraft(e.target.value);
                                  draftRef.current = e.target.value;
                                }}
                                onKeyDown={(e) => {
                                  if (e.key === "Escape") {
                                    e.preventDefault();
                                    closeEditor(s.session_id);
                                  }
                                }}
                                onBlur={() => {
                                  clearBlurTimer();
                                  blurTimerRef.current = setTimeout(() => {
                                    blurTimerRef.current = null;
                                    // Escape closed it, or another row took
                                    // over, or a submit is already running.
                                    if (editingIdRef.current !== s.session_id) return;
                                    if (savingIdRef.current === s.session_id) return;
                                    void commitRename(s);
                                  }, BLUR_COMMIT_DELAY_MS);
                                }}
                                aria-label="Conversation title"
                                aria-invalid={renameError ? true : undefined}
                                className="w-full rounded-md bg-surface-input border border-line-strong px-2 py-1.5 text-base sm:text-xs text-fg focus:outline-none focus:border-fg-subtle disabled:opacity-60"
                              />
                              {renameError && (
                                <p className="mt-1 text-[11px] text-red-400" role="alert">
                                  {renameError}
                                </p>
                              )}
                            </form>
                          ) : (
                            <button
                              type="button"
                              onClick={() => onSelect(s.session_id)}
                              className="flex-1 min-w-0 min-h-touch text-left px-2 py-2 pr-20 cursor-pointer"
                            >
                              <p className="text-xs truncate leading-snug">
                                {s.title || "Untitled chat"}
                              </p>
                              <p className="text-[10px] text-fg-subtle mt-0.5">
                                {formatRelativeTime(s.updated_at)}
                              </p>
                            </button>
                          )}
                          {!isEditing && (
                            <div className="absolute right-0.5 top-1/2 -translate-y-1/2 flex items-center">
                              <button
                                type="button"
                                aria-label="Rename chat"
                                title="Rename chat"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  startRename(s);
                                }}
                                className={`${ROW_ACTION_CLASS} hover:text-fg`}
                              >
                                <Icon name="pencil" size="w-4 h-4" />
                              </button>
                              <button
                                type="button"
                                aria-label="Delete chat"
                                title="Delete chat"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  onDelete(s.session_id);
                                }}
                                className={`${ROW_ACTION_CLASS} hover:text-red-400`}
                              >
                                <Icon name="trash" size="w-4 h-4" />
                              </button>
                            </div>
                          )}
                        </div>
                      );
                    })}
                    {hiddenCount > 0 && (
                      <button
                        type="button"
                        onClick={() => revealAll(group.key)}
                        className="w-full min-h-touch text-left px-2 py-1.5 text-[11px] text-fg-subtle hover:text-fg cursor-pointer"
                      >
                        Show {hiddenCount} more
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
