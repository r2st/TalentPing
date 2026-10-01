import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import RecruiterStats from "../components/RecruiterStats";
import { useConfirm } from "../components/ui/ConfirmDialog";
import ErrorBanner from "../components/ui/ErrorBanner";
import FilePreview from "../components/ui/FilePreview";
import useFilePreview from "../components/ui/useFilePreview";
import ShortcutHints from "../components/ui/ShortcutHints";
import SkeletonLoader from "../components/ui/SkeletonLoader";
import { useToast } from "../components/ui/Toast";
import { useListKeyboard } from "../hooks/useKeyboard";
import {
  APPLICATION_STATUS_STYLE,
  INTERVIEW_STAGE_STATUSES,
  RECRUITER_KIND_STYLE,
  REPLY_INTENTS,
  REPLY_INTENT_STYLE,
  REPLY_ROUTE_STYLE,
  REPLY_TEMPLATE_LABEL as TEMPLATE_LABEL,
  REPLY_TEMPLATE_STYLE as TEMPLATE_STYLE,
} from "../lib/constants";
import { notifyCountsChanged } from "../lib/events";
import { formatWhen } from "../lib/format";
import { api } from "../lib/api";

const EMPTY_FILTERS = {
  direction: "all",
  intent: "",
  unread: false,
  needs_reply: false,
  q: "",
};

/** Whether anything beyond the default All/all-directions view is narrowing the list. */
const isFiltered = (filters) =>
  filters.direction !== "all" ||
  Boolean(filters.intent || filters.unread || filters.needs_reply || filters.q);

/**
 * Inbox — every email, both directions, and everything waiting to go out.
 *
 * Three views of one queue. **Conversations** is thread-shaped and complete: the
 * outreach and follow-ups sent on the user's behalf, whatever recruiters wrote
 * back, classified, with the drafted reply inline. **Recruiter Inbox** is
 * message-shaped, because it holds the mail we *didn't* start — a recruiter who
 * wrote first has no conversation yet, only a message, a verdict about it and at
 * most one reply. **Drafts** is draft-shaped: every message awaiting approval in
 * one column, including outreach drafts that have no conversation yet.
 *
 * The conversation list used to require an inbound message, so a user who had
 * sent fifty applications and heard nothing yet saw an empty page. All | Sent |
 * Received now narrows it instead of the backend deciding for them.
 *
 * These used to be two pages, and a draft could be acted on from either — which
 * meant two places to check and two chances to send the same thing twice. All
 * three views go through the same review endpoints, so a draft has one state
 * whichever tab you are looking at. That is also why the Recruiter Inbox is a
 * tab here rather than a fifth destination in the nav: a separate page listing
 * the same drafts is exactly the mistake this consolidation fixed.
 */
const TABS = new Set(["replies", "recruiters", "drafts"]);

export default function Inbox() {
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab");
  const tab = TABS.has(requested) ? requested : "replies";

  const setTab = (next) =>
    setParams(next === "replies" ? {} : { tab: next }, { replace: true });

  if (tab === "drafts") return <DraftsView onTab={setTab} />;
  if (tab === "recruiters") return <RecruiterInboxView onTab={setTab} />;
  return <RepliesView onTab={setTab} />;
}

/* -------------------------------------------------------------------------- */
/* Shared chrome                                                              */
/* -------------------------------------------------------------------------- */

function ViewTabs({ tab, onTab, counts }) {
  const items = [
    ["replies", "Conversations", counts?.threads],
    // Badged with what needs the user, not the total: a hundred filed job
    // alerts are not a hundred things to do.
    ["recruiters", "Recruiter Inbox", counts?.recruiters],
    ["drafts", "Drafts", counts?.drafts],
  ];
  return (
    <div className="flex gap-1" role="tablist" aria-label="Inbox views">
      {items.map(([key, label, count]) => (
        <button
          key={key}
          role="tab"
          aria-selected={tab === key}
          onClick={() => onTab(key)}
          className={[
            "rounded-md px-3 py-1.5 text-sm transition-colors",
            tab === key
              ? "bg-white/[0.07] text-white"
              : "text-white/35 hover:text-white/70",
          ].join(" ")}
        >
          {label}
          {count > 0 && (
            <span className="ml-2 font-mono text-[10px] text-white/35">{count}</span>
          )}
        </button>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Conversations                                                              */
/* -------------------------------------------------------------------------- */

function RepliesView({ onTab }) {
  const [data, setData] = useState(null);
  const [draftCount, setDraftCount] = useState(0);
  const [recruiterCount, setRecruiterCount] = useState(0);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [selectedId, setSelectedId] = useState(null);
  // Bumped by the `r` shortcut; the draft editor watches it and opens itself.
  const [replyNonce, setReplyNonce] = useState(0);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState(null);
  const toast = useToast();

  const load = useCallback(async () => {
    const query = {
      direction: filters.direction,
      intent: filters.intent,
      unread: filters.unread ? "true" : "",
      needs_reply: filters.needs_reply ? "true" : "",
      q: filters.q,
    };
    setData(await api.inbox(query));
    // Reading a thread or acting on a draft moves the header badges too.
    notifyCountsChanged();
  }, [filters]);

  useEffect(() => {
    load().catch((err) => setError(err.message));
  }, [load]);

  // The other tabs' counts, so switching tabs isn't the only way to find out
  // something is waiting there. Both are best-effort: a failed count badge must
  // never take the conversation list down with it.
  useEffect(() => {
    api
      .review()
      .then((queue) => setDraftCount(queue?.count ?? 0))
      .catch(() => {});
    api
      .recruiterInbox()
      .then((inbox) => setRecruiterCount(inbox?.counts?.needs_you ?? 0))
      .catch(() => {});
  }, [data]);

  const threads = data?.threads ?? [];
  useListKeyboard({
    items: threads,
    idOf: (thread) => thread.thread_id,
    selectedId,
    onSelect: setSelectedId,
    onClose: () => setSelectedId(null),
    onReply: () => setReplyNonce((n) => n + 1),
    enabled: threads.length > 0,
  });

  async function sync() {
    setError(null);
    setSyncing(true);
    try {
      const result = await api.syncInbox();
      await load();
      // An inline sync is capped, so say what it left rather than implying the
      // whole mailbox was covered.
      const left = result.threads_skipped
        ? ` ${result.threads_skipped} left for the next check.`
        : "";
      toast.success(
        result.threads_polled === 0
          ? "Nothing to check yet."
          : result.dispatched
            ? `Checking ${result.threads_polled} conversations in the background.`
            : `Checked ${result.threads_polled} conversations.${left}`,
      );
    } catch (err) {
      setError(err.message);
      toast.error(err.message);
    } finally {
      setSyncing(false);
    }
  }

  // A failed first load must not leave the user staring at a skeleton forever —
  // show the reason, with a way to retry.
  if (!data)
    return error ? (
      <div className="space-y-4">
        <ErrorBanner onDismiss={() => setError(null)}>{error}</ErrorBanner>
        <button
          className="btn-ghost"
          onClick={() => load().catch((err) => setError(err.message))}
        >
          Try again
        </button>
      </div>
    ) : (
      <SkeletonLoader rows={[96, 320]} />
    );

  const { counts } = data;
  const filtered = isFiltered(filters);

  return (
    <div className="space-y-8">
      <header className="animate-fade-up">
        <p className="eyebrow">Inbox</p>
        <h1 className="mt-3 font-display text-4xl leading-none tracking-tightest text-white">
          {counts.unread > 0
            ? `${counts.unread} unread ${counts.unread === 1 ? "reply" : "replies"}`
            : counts.threads === 0
              ? "Nothing here yet."
              : "All caught up."}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-white/45">
          {counts.threads === 0
            ? "Every email goes here — the applications sent on your behalf and whatever comes back. Nothing has been sent yet."
            : `${counts.threads} ${counts.threads === 1 ? "conversation" : "conversations"} · ${counts.sent} sent · ${counts.received} answered${
                counts.awaiting_reply > 0
                  ? ` · ${counts.awaiting_reply} with a draft waiting`
                  : ""
              }`}
        </p>
      </header>

      <ErrorBanner onDismiss={() => setError(null)}>{error}</ErrorBanner>

      <ViewTabs
        tab="replies"
        onTab={onTab}
        counts={{
          threads: counts.threads,
          drafts: draftCount,
          recruiters: recruiterCount,
        }}
      />

      <Toolbar
        counts={counts}
        filters={filters}
        onChange={setFilters}
        onClear={() => setFilters(EMPTY_FILTERS)}
        onSync={sync}
        syncing={syncing}
      />

      {threads.length === 0 ? (
        <EmptyState
          filtered={filtered}
          onClear={() => setFilters(EMPTY_FILTERS)}
          onSync={sync}
          syncing={syncing}
        />
      ) : (
        <>
          <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
            <ThreadList
              threads={threads}
              selectedId={selectedId}
              onSelect={setSelectedId}
              className={selectedId ? "hidden lg:block" : ""}
            />
            <Conversation
              key={selectedId}
              threadId={selectedId}
              replyNonce={replyNonce}
              onBack={() => setSelectedId(null)}
              onChange={load}
              onError={setError}
            />
          </div>
          <ShortcutHints />
        </>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

/** All | Sent | Received — which side of the mailbox the list is showing. */
function DirectionTabs({ value, counts, onChange }) {
  const items = [
    ["all", "All", counts.threads],
    ["sent", "Sent", counts.sent],
    ["received", "Received", counts.received],
  ];
  return (
    <div className="flex gap-1" role="tablist" aria-label="Mail direction">
      {items.map(([key, label, count]) => (
        <button
          key={key}
          role="tab"
          aria-selected={value === key}
          onClick={() => onChange(key)}
          className={[
            "rounded-md px-3 py-1.5 text-sm transition-colors",
            value === key
              ? "bg-white/[0.07] text-white"
              : "text-white/35 hover:text-white/70",
          ].join(" ")}
        >
          {label}
          <span className="ml-2 font-mono text-[10px] text-white/35">{count ?? 0}</span>
        </button>
      ))}
    </div>
  );
}

function Toolbar({ counts, filters, onChange, onClear, onSync, syncing }) {
  const set = (patch) => onChange({ ...filters, ...patch });
  const active = isFiltered(filters);

  return (
    <div className="space-y-3">
      <DirectionTabs
        value={filters.direction}
        counts={counts}
        onChange={(direction) => set({ direction })}
      />

      <div className="flex flex-wrap items-center gap-2">
        <FilterChip
          active={!filters.unread && !filters.needs_reply && !filters.intent}
          onClick={() => set({ intent: "", unread: false, needs_reply: false })}
        >
          Everything <Count value={counts.threads} />
        </FilterChip>
        <FilterChip
          active={filters.unread}
          onClick={() => set({ unread: !filters.unread })}
        >
          Unread <Count value={counts.unread} />
        </FilterChip>
        <FilterChip
          active={filters.needs_reply}
          onClick={() => set({ needs_reply: !filters.needs_reply })}
        >
          Draft waiting <Count value={counts.awaiting_reply} />
        </FilterChip>

        <span className="mx-1 hidden h-4 w-px bg-white/10 sm:block" />

        {REPLY_INTENTS.filter((intent) => counts.by_intent?.[intent]).map((intent) => {
          const [label] = REPLY_INTENT_STYLE[intent];
          return (
            <FilterChip
              key={intent}
              active={filters.intent === intent}
              onClick={() => set({ intent: filters.intent === intent ? "" : intent })}
            >
              {label} <Count value={counts.by_intent[intent]} />
            </FilterChip>
          );
        })}

        <button className="btn-quiet ml-auto" onClick={onSync} disabled={syncing}>
          {syncing ? "Checking…" : "Check for new mail"}
        </button>
      </div>

      <div className="flex items-center gap-2">
        <input
          className="input max-w-xs"
          type="search"
          aria-label="Search conversations"
          placeholder="Search company, recruiter or subject"
          value={filters.q}
          onChange={(e) => set({ q: e.target.value })}
        />
        {active && (
          <button className="btn-quiet" onClick={onClear}>
            Clear
          </button>
        )}
      </div>
    </div>
  );
}

function FilterChip({ active, onClick, children }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={[
        "chip transition-colors",
        active ? "border-signal/40 bg-signal/10 text-white" : "text-white/50 hover:text-white/80",
      ].join(" ")}
    >
      {children}
    </button>
  );
}

function Count({ value }) {
  return <span className="font-mono text-[10px] text-white/35">{value ?? 0}</span>;
}

function ThreadList({ threads, selectedId, onSelect, className = "" }) {
  return (
    <ul className={["space-y-2", className].join(" ")} aria-label="Conversations">
      {threads.map((thread) => {
        const selected = thread.thread_id === selectedId;
        const [intentLabel, intentTone] =
          REPLY_INTENT_STYLE[thread.last_intent] ?? REPLY_INTENT_STYLE.OTHER;

        return (
          <li key={thread.thread_id}>
            <button
              className={[
                "panel w-full space-y-2 px-4 py-3 text-left transition-colors",
                selected ? "border-signal/40 bg-white/[0.04]" : "hover:bg-white/[0.02]",
              ].join(" ")}
              onClick={() => onSelect(thread.thread_id)}
              aria-current={selected ? "true" : undefined}
            >
              <div className="flex items-center gap-2">
                {thread.unread_count > 0 && (
                  <span
                    className="h-1.5 w-1.5 shrink-0 rounded-full bg-signal"
                    aria-hidden="true"
                  />
                )}
                <span
                  className={[
                    "min-w-0 flex-1 truncate text-sm",
                    thread.unread_count > 0 ? "font-medium text-white" : "text-white/75",
                  ].join(" ")}
                >
                  {thread.company || thread.recruiter_name || "Unknown company"}
                  {/* The dot is decorative; the state has to reach a screen
                      reader some other way. Kept after the name so it reads as
                      "Acme, unread" rather than shadowing the company. */}
                  {thread.unread_count > 0 && <span className="sr-only">, unread</span>}
                </span>
                <span className="shrink-0 font-mono text-[10px] text-white/30">
                  {formatWhen(thread.last_activity_at || thread.last_message_at)}
                </span>
              </div>

              {thread.role && (
                <p className="truncate text-xs text-white/40">{thread.role}</p>
              )}

              <p className="line-clamp-2 text-xs leading-relaxed text-white/40">
                {thread.snippet || "No message body."}
              </p>

              <div className="flex flex-wrap items-center gap-1.5">
                {/* What the last message on the thread was: an outreach or
                    follow-up we sent, or a classified reply. */}
                {thread.last_direction === "SENT" && (
                  <span className="badge bg-white/10 text-white/60">sent</span>
                )}
                {thread.last_intent && (
                  <span className={`badge ${intentTone}`}>{intentLabel}</span>
                )}
                {thread.draft_email_id != null && (
                  <span className="badge bg-signal/15 text-signal">draft ready</span>
                )}
              </div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The conversation pane. Loads its own thread so the list stays cheap, and
 * marks the thread read on open — the badge in the nav clears as a result.
 */
function Conversation({ threadId, replyNonce, onBack, onChange, onError }) {
  const [thread, setThread] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (threadId == null) return;
    setLoading(true);
    try {
      const detail = await api.inboxThread(threadId);
      setThread(detail);
      if (detail.unread_count > 0) {
        await api.markThreadRead(threadId);
        await onChange();
      }
    } catch (err) {
      onError(err.message);
    } finally {
      setLoading(false);
    }
  }, [threadId, onChange, onError]);

  useEffect(() => {
    load();
  }, [load]);

  if (threadId == null) {
    return (
      <div className="panel hidden items-center justify-center px-6 py-20 text-center text-sm text-white/30 lg:flex">
        Select a conversation to read it.
      </div>
    );
  }

  if (!thread) {
    return loading ? <SkeletonLoader rows={[320]} /> : null;
  }

  const [statusLabel, statusTone] =
    APPLICATION_STATUS_STYLE[thread.application_status] ?? [
      thread.application_status,
      "bg-white/[0.06] text-white/45",
    ];
  const draft = thread.messages.find((m) => m.is_draft) ?? null;
  const history = thread.messages.filter((m) => !m.is_draft);

  return (
    <div className="space-y-4">
      <section className="panel overflow-hidden">
        <header className="space-y-2 border-b px-5 py-4 hairline">
          {/* On a phone the list is replaced rather than beside us, so Back gets
              its own row instead of squeezing the company name. */}
          <button className="btn-quiet -ml-2 lg:hidden" onClick={onBack}>
            ← All conversations
          </button>
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <p className="truncate font-display text-xl tracking-tight text-white">
                {thread.company || thread.recruiter_name || "Conversation"}
              </p>
              <p className="mt-0.5 truncate text-xs text-white/40">
                {thread.role ? `${thread.role} · ` : ""}
                <span className="font-mono">{thread.recruiter_email}</span>
              </p>
            </div>
            <span className={`badge shrink-0 ${statusTone}`}>{statusLabel}</span>
          </div>
          {thread.subject && (
            <p className="truncate text-sm text-white/60">{thread.subject}</p>
          )}
        </header>

        <ol
          className="max-h-[32rem] space-y-4 overflow-y-auto px-5 py-5"
          aria-label="Messages"
        >
          {history.map((message) => (
            <Message key={message.id} message={message} />
          ))}
        </ol>

        {draft ? (
          <DraftReply
            draft={draft}
            to={thread.recruiter_email}
            replyNonce={replyNonce}
            onChange={async () => {
              await load();
              await onChange();
            }}
            onError={onError}
          />
        ) : (
          <p className="border-t px-5 py-4 text-xs text-white/30 hairline">
            No reply drafted for this conversation.
          </p>
        )}
      </section>

      {INTERVIEW_STAGE_STATUSES.has(thread.application_status) && (
        <InterviewPrep
          applicationId={thread.application_id}
          company={thread.company}
          role={thread.role}
          onError={onError}
        />
      )}
    </div>
  );
}

function Message({ message }) {
  const inbound = message.direction === "RECEIVED";
  const [intentLabel, intentTone] =
    REPLY_INTENT_STYLE[message.intent] ?? REPLY_INTENT_STYLE.OTHER;

  return (
    <li className={inbound ? "" : "lg:pl-10"}>
      <div
        className={[
          "rounded-lg border px-4 py-3 hairline",
          inbound ? "bg-white/[0.03]" : "bg-signal/[0.04]",
        ].join(" ")}
      >
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="eyebrow">{inbound ? "They wrote" : "You sent"}</span>
          <span className="min-w-0 truncate font-mono text-[11px] text-white/40">
            {inbound ? message.from_address : message.to_address}
          </span>
          {inbound ? (
            message.intent && (
              <span className={`badge ${intentTone}`}>{intentLabel}</span>
            )
          ) : (
            // Outbound mail is labelled by what became of it: queued and failed
            // sends are on the thread too, and reading "sent" for either would
            // be a lie.
            <span
              className={[
                "badge",
                message.status === "FAILED"
                  ? "bg-bad/10 text-bad/70"
                  : "bg-white/10 text-white/60",
              ].join(" ")}
            >
              {message.status === "SENT" ? "sent" : message.status.toLowerCase()}
            </span>
          )}
          <span className="ml-auto shrink-0 font-mono text-[10px] text-white/25">
            {formatWhen(message.sent_at || message.created_at)}
          </span>
        </div>
        {message.subject && (
          <p className="mt-2 text-sm font-medium text-white/85">{message.subject}</p>
        )}
        <p className="mt-1.5 whitespace-pre-wrap text-sm leading-relaxed text-white/65">
          {message.body_text}
        </p>
        {/* The API has always said what a message carried; the conversation was
            the one place that dropped it, so a sent application read as though
            it went out bare. */}
        <Attachments
          files={message.attachments}
          note={message.attachment_note}
          emailId={message.id}
          className="mt-2"
        />
      </div>
    </li>
  );
}

/**
 * A named attachment that opens when you click it.
 *
 * `index` is the position the server listed the file at, and the same position
 * it resolves the bytes at — the list and the file come off one resolver, so a
 * badge always opens the document it names.
 */
function AttachmentBadge({ name, emailId, index, onRemove, disabled = false }) {
  const toast = useToast();
  // The blobs outlive the fetch, so they are released when the preview closes or
  // the row unmounts — not when the click handler returns. A recruiter's .docx
  // costs a second request for the server's rendering of it; a PDF doesn't.
  const preview = useFilePreview({
    name,
    fetchFile: () => api.emailAttachment(emailId, index),
    fetchPreview: () => api.emailAttachmentPreview(emailId, index),
    onError: (err) => toast.error(err.message),
  });

  // Nothing to fetch against: keep the name on screen rather than offering a
  // button that can only fail.
  if (emailId == null) {
    return (
      <span className="badge bg-white/[0.06] font-mono text-white/55">{name}</span>
    );
  }

  return (
    <>
      {/* One control when the file can only be read, two when it can also be
          taken off. They share a pill so the × reads as belonging to this file
          rather than to the row. */}
      <span className="inline-flex items-center overflow-hidden rounded-full bg-white/[0.06]">
        <button
          type="button"
          className="px-2.5 py-0.5 font-mono text-[11px] text-white/55 transition hover:bg-white/[0.08] hover:text-white/80 disabled:opacity-50"
          onClick={preview.open}
          disabled={preview.busy}
          title={`Preview ${name}`}
        >
          {preview.busy ? "opening…" : name}
        </button>
        {onRemove && (
          <button
            type="button"
            className="border-l border-white/10 px-2 py-0.5 text-[11px] leading-none text-white/40 transition hover:bg-bad/20 hover:text-bad disabled:opacity-40"
            onClick={onRemove}
            disabled={disabled}
            aria-label={`Remove ${name}`}
            title={`Remove ${name}`}
          >
            ×
          </button>
        )}
      </span>
      {preview.url && (
        <FilePreview
          name={name}
          url={preview.url}
          previewUrl={preview.previewUrl}
          onClose={preview.close}
        />
      )}
    </>
  );
}

/**
 * What will travel with a draft when it is approved.
 *
 * Attachments are resolved at send time — the resume that goes out should be
 * the one as it stands on the day, not as it stood when the draft was written —
 * which left a reviewer looking at a reply with no way to tell whether a resume
 * was coming. "It drafted a reply but I don't see the attachment" and "it
 * drafted a reply with no attachment" looked identical, and only one of them is
 * a problem. This says which, and — given `emailId` — opens the document itself.
 *
 * `note` is the server's sentence for why no resume is queued. Shown in the
 * warning colour, because it means the message is about to go out without the
 * document that makes it an application.
 */
function Attachments({ files, note, emailId, className = "px-5 pt-2" }) {
  if (!files?.length && !note) return null;
  return (
    <div className={`flex flex-wrap items-center gap-2 ${className}`}>
      <span className="eyebrow">Attached</span>
      {files?.map((name, index) => (
        // Two attachments can share a name (a resume and a letter both rendered
        // for the same person), so position is what identifies one.
        <AttachmentBadge
          key={`${index}-${name}`}
          name={name}
          emailId={emailId}
          index={index}
        />
      ))}
      {note && <span className="text-[11px] text-warn">{note}</span>}
    </div>
  );
}

/**
 * What a draft will carry — and the controls to change it.
 *
 * The read-only version of this told the user which resume was queued, which
 * turned out to be half an answer: knowing the wrong CV is about to go out is
 * only useful if you can do something about it. So the same list is now
 * editable. Every file can be removed, any of the candidate's resumes can be
 * pinned in place of the resolved one, and arbitrary files can be added for the
 * recruiter who asks for a portfolio or a signed offer.
 *
 * State comes back whole from every mutation — the server returns the new plan,
 * and this renders it — rather than being patched locally. Attachments are
 * resolved, not stored, so a local guess at what removing one leaves behind
 * would be a second implementation of the resolver, and the preview endpoint
 * addresses files by position: a list that disagreed with the server's by even
 * one entry would open the wrong document.
 */
function DraftAttachments({ emailId, initialFiles = [], note, onError }) {
  const toast = useToast();
  const fileRef = useRef(null);
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .emailAttachments(emailId)
      // Best-effort: a failed load leaves the names from the thread payload on
      // screen rather than blanking the row.
      .then((next) => live && setData(next))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [emailId]);

  async function run(fn, successMessage) {
    if (busy) return;
    setBusy(true);
    try {
      setData(await fn());
      if (successMessage) toast.success(successMessage);
    } catch (err) {
      onError?.(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  // Until the fetch lands, show what the thread payload already said. The names
  // are the same ones and in the same order — only the controls are missing.
  const files =
    data?.files ??
    initialFiles.map((name, index) => ({ index, filename: name, kind: "resume" }));
  const editable = data?.editable ?? false;
  const message = data ? data.note : note;

  return (
    <div className="space-y-2 px-5 pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="eyebrow">Attached</span>
        {files.map((file) => (
          <AttachmentBadge
            key={`${file.index}-${file.filename}`}
            name={file.filename}
            emailId={emailId}
            index={file.index}
            disabled={busy}
            onRemove={
              editable
                ? () =>
                    run(
                      () => api.removeEmailAttachment(emailId, file.index),
                      `Removed ${file.filename}.`,
                    )
                : undefined
            }
          />
        ))}
        {files.length === 0 && !message && (
          <span className="text-[11px] text-white/30">Nothing attached.</span>
        )}
        {message && <span className="text-[11px] text-warn">{message}</span>}
        {editable && (
          <button
            type="button"
            className="btn-quiet ml-auto text-[11px]"
            onClick={() => setOpen((on) => !on)}
            aria-expanded={open}
          >
            {open ? "Done" : "Change files"}
          </button>
        )}
      </div>

      {editable && open && (
        <div className="space-y-3 rounded-lg border bg-ink-900/40 px-4 py-3 hairline">
          {data.resume_options.length > 0 && (
            <label className="block space-y-1">
              <span className="eyebrow">Send this resume</span>
              <select
                className="input text-sm"
                // "" is the pipeline's choice, which is a real option rather
                // than an absence — it is what every untouched draft uses.
                value={data.resume_id ?? ""}
                disabled={busy}
                onChange={(event) => {
                  const value = event.target.value;
                  run(
                    () =>
                      api.setEmailResume(emailId, value === "" ? null : Number(value)),
                    "Resume updated.",
                  );
                }}
              >
                <option value="">Choose for me (best match)</option>
                {data.resume_options.map((resume) => (
                  <option key={resume.id} value={resume.id}>
                    {resume.label}
                    {resume.filename ? ` — ${resume.filename}` : ""}
                    {resume.is_default ? " (default)" : ""}
                  </option>
                ))}
              </select>
              {/* A resume uploaded before the file itself was kept can still be
                  sent, but as a reconstruction. Worth saying: it is the reason
                  the document may not look like theirs. */}
              {data.resume_options.some((r) => !r.has_original_file) && (
                <span className="block text-[11px] leading-relaxed text-white/35">
                  Resumes uploaded before this update are rebuilt from their text
                  rather than sent as your original file. Re-upload one to send it
                  exactly as it is.
                </span>
              )}
            </label>
          )}

          {data.resume_removed && (
            <p className="text-[11px] text-warn">
              No resume will be sent. Pick one above to put it back.
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <input
              ref={fileRef}
              type="file"
              className="hidden"
              aria-label="Add an attachment"
              onChange={(event) => {
                const file = event.target.files?.[0];
                // Cleared straight away so re-choosing the same file after a
                // failure still fires a change event.
                event.target.value = "";
                if (file)
                  run(
                    () => api.addEmailAttachment(emailId, file),
                    `Attached ${file.name}.`,
                  );
              }}
            />
            <button
              type="button"
              className="btn-ghost text-[11px]"
              onClick={() => fileRef.current?.click()}
              disabled={busy}
            >
              {busy ? "Working…" : "Add a file"}
            </button>
            <span className="text-[11px] text-white/30">
              Up to 15 MB each — a portfolio, a transcript, anything they asked for.
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * The draft the reply agent wrote, inline at the foot of the conversation.
 * Same contract as the Drafts tab: read it, edit it if you like, then approve
 * or discard. Nothing goes out unapproved.
 */
function DraftReply({ draft, to, replyNonce, onChange, onError }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [subject, setSubject] = useState(draft.subject || "");
  const [body, setBody] = useState(draft.body_text || "");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const bodyRef = useRef(null);

  const dirty = subject !== (draft.subject || "") || body !== (draft.body_text || "");

  // The `r` shortcut lands here: open the editor and put the cursor in it, so
  // "r" to reply behaves the way it does in every mail client.
  useEffect(() => {
    if (!replyNonce) return;
    setEditing(true);
  }, [replyNonce]);

  useEffect(() => {
    if (editing && replyNonce) bodyRef.current?.focus();
  }, [editing, replyNonce]);

  async function save() {
    onError(null);
    setBusy(true);
    try {
      await api.editEmail(draft.id, { subject, body_text: body });
      setEditing(false);
      toast.success("Draft saved.");
      await onChange();
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (
      !(await confirm({
        title: "Send this reply?",
        message: `It will go out from your inbox to ${to}. This can't be recalled.`,
        confirmLabel: "Approve & send",
      }))
    )
      return;
    await run(api.approveDraft, "Reply sent.");
  }

  async function discard() {
    if (
      !(await confirm({
        title: "Discard this draft?",
        message: "The draft will be deleted and never sent. This can't be undone.",
        confirmLabel: "Discard draft",
        tone: "danger",
      }))
    )
      return;
    await run(api.dismissDraft, "Draft discarded.");
  }

  async function run(fn, successMessage) {
    onError(null);
    setBusy(true);
    try {
      if (dirty) await api.editEmail(draft.id, { subject, body_text: body });
      await fn(draft.id);
      toast.success(successMessage);
      await onChange();
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border-t bg-white/[0.02] hairline">
      <div className="flex flex-wrap items-center gap-2 px-5 pt-4">
        {/* "Scout suggests" rather than "draft reply" once we know which brief
            it was written to: the label is the difference between a mystery
            block of text and one the user can judge before reading it. */}
        <span className="badge bg-signal/15 text-signal">
          {draft.draft_template ? "Scout suggests" : "draft reply"}
        </span>
        {draft.draft_template && (
          <span className={`badge ${TEMPLATE_STYLE[draft.draft_template] ?? "bg-white/[0.06] text-white/45"}`}>
            {TEMPLATE_LABEL[draft.draft_template] ?? draft.draft_template.toLowerCase()}
          </span>
        )}
        <span className="font-mono text-[11px] text-white/30">→ {to}</span>
      </div>

      {draft.draft_note && (
        <p className="px-5 pt-2 text-[11px] leading-relaxed text-white/45">
          {draft.draft_note}
        </p>
      )}

      <DraftAttachments
        emailId={draft.id}
        initialFiles={draft.attachments}
        note={draft.attachment_note}
        onError={onError}
      />

      <div className="space-y-3 px-5 py-4">
        {editing ? (
          <>
            <input
              className="input font-medium"
              aria-label="Draft subject"
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="Subject"
            />
            <textarea
              ref={bodyRef}
              className="input min-h-[160px] font-mono text-[13px] leading-relaxed"
              aria-label="Draft body"
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
          </>
        ) : (
          <>
            <p className="text-sm font-medium text-white">{subject}</p>
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-white/70">
              {body}
            </p>
          </>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t px-5 py-3 hairline">
        <button className="btn-primary" onClick={approve} disabled={busy}>
          {busy ? "Working…" : "Approve & send"}
        </button>
        {editing ? (
          <button className="btn-ghost" onClick={save} disabled={busy || !dirty}>
            Save edits
          </button>
        ) : (
          <button className="btn-ghost" onClick={() => setEditing(true)} disabled={busy}>
            Edit
          </button>
        )}
        <button className="btn-quiet ml-auto hover:text-bad" onClick={discard} disabled={busy}>
          Discard
        </button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Interview prep                                                             */
/* -------------------------------------------------------------------------- */

/**
 * Once a recruiter says yes, the automation is done and the candidate has to
 * perform. This is the briefing for that: what the posting says about the
 * company, the questions its own requirements imply, what to lead with, and —
 * the part worth reading twice — what the resume does *not* cover.
 *
 * Generated on demand rather than on open: it costs a model call, and most
 * visits to a scheduling thread are to read the reply, not to prepare.
 */
function InterviewPrep({ applicationId, company, role, onError }) {
  const [prep, setPrep] = useState(null);
  const [busy, setBusy] = useState(false);

  async function generate() {
    setBusy(true);
    try {
      setPrep(await api.interviewPrep(applicationId));
    } catch (err) {
      onError(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (!prep) {
    return (
      <section className="panel space-y-3 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="eyebrow">Interview prep</p>
            <p className="mt-2 max-w-md text-sm leading-relaxed text-white/45">
              They want to talk. Get a briefing on {company || "this company"}
              {role ? ` for the ${role} role` : ""} — likely questions, what to lead
              with, and the gaps worth rehearsing.
            </p>
          </div>
          <button className="btn-primary shrink-0" onClick={generate} disabled={busy}>
            {busy ? "Preparing…" : "Prepare me"}
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="panel space-y-6 p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="eyebrow">Interview prep</p>
        <div className="flex items-center gap-2">
          <span className="badge bg-white/[0.06] text-white/40">
            {prep.generated_with === "llm" ? "ai-written" : "from the posting"}
          </span>
          <button className="btn-quiet" onClick={generate} disabled={busy}>
            {busy ? "Working…" : "Regenerate"}
          </button>
        </div>
      </div>

      <div className="space-y-3">
        <p className="text-sm leading-relaxed text-white/75">{prep.company_research}</p>
        {prep.company_facts?.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {prep.company_facts.map((fact) => (
              <span key={fact} className="chip text-white/50">
                {fact}
              </span>
            ))}
          </div>
        )}
      </div>

      {prep.questions?.length > 0 && (
        <PrepSection title="Questions to expect">
          <ol className="space-y-2.5">
            {prep.questions.map((item, index) => (
              <li key={`${item.question}-${index}`} className="flex gap-3">
                <span className="mt-0.5 font-mono text-[10px] text-white/25">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <div className="min-w-0">
                  <p className="text-sm text-white/80">{item.question}</p>
                  {item.why && (
                    <p className="mt-0.5 text-xs leading-relaxed text-white/35">
                      {item.why}
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </PrepSection>
      )}

      {prep.talking_points?.length > 0 && (
        <PrepSection title="Lead with these">
          <ul className="space-y-2">
            {prep.talking_points.map((item, index) => (
              <li
                key={`${item.point}-${index}`}
                className="rounded-lg border bg-ink-900/40 px-4 py-3 hairline"
              >
                <p className="text-sm text-white/80">{item.point}</p>
                {item.evidence && (
                  <p className="mt-1 font-mono text-[11px] text-white/35">
                    from your resume: {item.evidence}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </PrepSection>
      )}

      {prep.gaps?.length > 0 && (
        <PrepSection title="Rehearse the gaps">
          <ul className="space-y-2">
            {prep.gaps.map((gap) => (
              <li
                key={gap}
                className="rounded-lg border border-warn/25 bg-warn/[0.05] px-4 py-3 text-sm leading-relaxed text-white/70"
              >
                {gap}
              </li>
            ))}
          </ul>
        </PrepSection>
      )}

      {prep.questions_to_ask?.length > 0 && (
        <PrepSection title="Ask them">
          <ul className="space-y-1.5">
            {prep.questions_to_ask.map((question) => (
              <li key={question} className="flex gap-2 text-sm text-white/70">
                <span className="text-white/20">—</span>
                <span>{question}</span>
              </li>
            ))}
          </ul>
        </PrepSection>
      )}
    </section>
  );
}

function PrepSection({ title, children }) {
  return (
    <div className="space-y-3 border-t pt-5 hairline">
      <p className="eyebrow">{title}</p>
      {children}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Recruiter Inbox                                                            */
/* -------------------------------------------------------------------------- */

const RECRUITER_VIEWS = [
  ["all", "Everything", "detected"],
  ["needs_you", "Needs you", "needs_you"],
  ["drafted", "Drafted", "drafted"],
  ["replied", "Replied", "replied"],
  ["not_recruiter", "Filed", "not_recruiter"],
];

/**
 * Mail we didn't start: a recruiter wrote first, and this is what we made of it.
 *
 * Message-shaped rather than thread-shaped, because at this point there is no
 * thread — one email, a classification, the profile it matched and at most one
 * reply. Once the user sends that reply the conversation becomes an ordinary
 * thread and moves to the Conversations tab, which is why nothing here tries to
 * render a history.
 */
function RecruiterInboxView({ onTab }) {
  const [data, setData] = useState(null);
  const [threadCount, setThreadCount] = useState(0);
  const [draftCount, setDraftCount] = useState(0);
  const [view, setView] = useState("all");
  const [selectedId, setSelectedId] = useState(null);
  const [scanning, setScanning] = useState(false);
  const [showStats, setShowStats] = useState(false);
  const [error, setError] = useState(null);
  const toast = useToast();

  const load = useCallback(async () => {
    setData(await api.recruiterInbox({ view }));
    notifyCountsChanged();
  }, [view]);

  useEffect(() => {
    load().catch((err) => setError(err.message));
  }, [load]);

  useEffect(() => {
    api
      .inbox()
      .then((inbox) => setThreadCount(inbox?.counts?.threads ?? 0))
      .catch(() => {});
    api
      .review()
      .then((queue) => setDraftCount(queue?.count ?? 0))
      .catch(() => {});
  }, [data]);

  async function scan() {
    setError(null);
    setScanning(true);
    try {
      const result = await api.scanRecruiterInbox();
      await load();
      // A capped scan says what it left rather than implying the whole mailbox
      // was covered — the same contract the conversation sync keeps.
      const left = result.deferred
        ? ` ${result.deferred} left for the next check.`
        : "";
      // "Nothing new" is ambiguous when the mailbox plainly has mail in it: the
      // honest version says how much was looked at and that it had all been
      // seen before, so an empty result reads as "up to date" rather than as
      // "something is broken".
      const nothingNew = result.skipped_known
        ? `Nothing new — all ${result.skipped_known} of the messages in range have been checked before.`
        : "Nothing new from a recruiter.";
      toast.success(
        result.dispatched
          ? "Checking your inbox in the background."
          : result.detected === 0
            ? nothingNew
            : `Found ${result.detected} new ${result.detected === 1 ? "message" : "messages"}.${left}`,
      );
    } catch (err) {
      setError(err.message);
      toast.error(err.message);
    } finally {
      setScanning(false);
    }
  }

  if (!data)
    return error ? (
      <div className="space-y-4">
        <ErrorBanner onDismiss={() => setError(null)}>{error}</ErrorBanner>
        <button
          className="btn-ghost"
          onClick={() => load().catch((err) => setError(err.message))}
        >
          Try again
        </button>
      </div>
    ) : (
      <SkeletonLoader rows={[96, 320]} />
    );

  const { counts } = data;
  const emails = data.emails ?? [];
  // Three different empties, because the fix differs: the feature is off, it is
  // on but has found nothing yet, or the filter is too narrow.
  const off = !data.server_enabled || !data.enabled;

  return (
    <div className="space-y-8">
      <header className="animate-fade-up">
        <p className="eyebrow">Recruiter Inbox</p>
        <h1 className="mt-3 font-display text-4xl leading-none tracking-tightest text-white">
          {counts.needs_you > 0
            ? `${counts.needs_you} ${counts.needs_you === 1 ? "message needs" : "messages need"} you`
            : counts.detected === 0
              ? "Nothing here yet."
              : "All caught up."}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-white/45">
          {counts.detected === 0
            ? "When a recruiter emails you out of the blue, it shows up here — classified, matched to one of your profiles, with a reply ready to review."
            : `${counts.detected} detected · ${counts.drafted} drafted · ${counts.replied} replied · ${counts.not_recruiter} filed`}
        </p>
      </header>

      <ErrorBanner onDismiss={() => setError(null)}>{error}</ErrorBanner>

      <ViewTabs
        tab="recruiters"
        onTab={onTab}
        counts={{
          threads: threadCount,
          drafts: draftCount,
          recruiters: counts.needs_you,
        }}
      />

      <div className="flex flex-wrap items-center gap-2">
        {RECRUITER_VIEWS.map(([key, label, countKey]) => (
          <FilterChip
            key={key}
            active={view === key}
            onClick={() => {
              setView(key);
              setSelectedId(null);
            }}
          >
            {label} <Count value={counts[countKey]} />
          </FilterChip>
        ))}
        <button
          className="btn-quiet ml-auto"
          onClick={() => setShowStats((on) => !on)}
          aria-expanded={showStats}
        >
          {showStats ? "Hide activity" : "Activity"}
        </button>
        <button
          className="btn-quiet"
          onClick={scan}
          disabled={scanning || off}
          title={off ? "Turn on inbox watching in setup first" : undefined}
        >
          {scanning ? "Checking…" : "Check now"}
        </button>
      </div>

      {/* Collapsed by default: the messages are the page, and a user opening
          the tab wants the one that needs them, not a dashboard. */}
      {showStats && !off && <RecruiterStats onError={setError} />}

      {off ? (
        <RecruiterInboxOff serverEnabled={data.server_enabled} />
      ) : emails.length === 0 ? (
        <div className="panel px-6 py-16 text-center">
          <p className="font-display text-2xl tracking-tight text-white/80">
            {view === "all" ? "Nothing detected yet" : "Nothing in this view"}
          </p>
          <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-white/40">
            {view === "all"
              ? "Your inbox is being watched. The next recruiter email that arrives will land here within about fifteen minutes."
              : "Try another filter to see the rest of what came in."}
          </p>
          {view !== "all" && (
            <button className="btn-ghost mt-6" onClick={() => setView("all")}>
              Show everything
            </button>
          )}
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
          <RecruiterEmailList
            emails={emails}
            selectedId={selectedId}
            onSelect={setSelectedId}
            className={selectedId ? "hidden lg:block" : ""}
          />
          <RecruiterEmailDetail
            key={selectedId}
            emailId={selectedId}
            onBack={() => setSelectedId(null)}
            onChange={load}
            onError={setError}
          />
        </div>
      )}
    </div>
  );
}

/** The feature is off — say which switch, and where it is. */
function RecruiterInboxOff({ serverEnabled }) {
  return (
    <div className="panel px-6 py-16 text-center">
      <p className="font-display text-2xl tracking-tight text-white/80">
        Inbox watching is off
      </p>
      <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-white/40">
        {serverEnabled
          ? "Turn it on and AutoApply will read new mail in your inbox, spot the recruiters among the job alerts, and draft a reply against the profile that fits best. Nothing is sent without your say-so."
          : "This feature isn't enabled on this server yet. Once it is, you'll be able to switch it on here."}
      </p>
      {serverEnabled && (
        <Link className="btn-primary mt-6 inline-block" to="/setup">
          Turn it on in setup
        </Link>
      )}
    </div>
  );
}

function RecruiterEmailList({ emails, selectedId, onSelect, className = "" }) {
  return (
    <ul className={["space-y-2", className].join(" ")} aria-label="Detected mail">
      {emails.map((email) => {
        const selected = email.id === selectedId;
        const [kindLabel, kindTone] =
          RECRUITER_KIND_STYLE[email.kind] ?? RECRUITER_KIND_STYLE.UNKNOWN;
        const routeStyle = email.route ? REPLY_ROUTE_STYLE[email.route] : null;

        return (
          <li key={email.id}>
            <button
              className={[
                "panel w-full space-y-2 px-4 py-3 text-left transition-colors",
                selected ? "border-signal/40 bg-white/[0.04]" : "hover:bg-white/[0.02]",
              ].join(" ")}
              onClick={() => onSelect(email.id)}
              aria-current={selected ? "true" : undefined}
            >
              <div className="flex items-center gap-2">
                {!email.read_at && (
                  <span
                    className="h-1.5 w-1.5 shrink-0 rounded-full bg-signal"
                    aria-hidden="true"
                  />
                )}
                <span
                  className={[
                    "min-w-0 flex-1 truncate text-sm",
                    email.read_at ? "text-white/75" : "font-medium text-white",
                  ].join(" ")}
                >
                  {email.from_name || email.from_address}
                  {!email.read_at && <span className="sr-only">, unread</span>}
                </span>
                <span className="shrink-0 font-mono text-[10px] text-white/30">
                  {formatWhen(email.received_at || email.created_at)}
                </span>
              </div>

              {email.subject && (
                <p className="truncate text-xs text-white/40">{email.subject}</p>
              )}

              <p className="line-clamp-2 text-xs leading-relaxed text-white/40">
                {email.snippet || "No message body."}
              </p>

              <div className="flex flex-wrap items-center gap-1.5">
                <span className={`badge ${kindTone}`}>{kindLabel}</span>
                {routeStyle && (
                  <span className={`badge ${routeStyle[1]}`}>{routeStyle[0]}</span>
                )}
                {/* A recruiter we already answered who has come back. Its
                    status still reads "replied", which is true and is not what
                    the user needs to know at a glance. */}
                {email.escalated && (
                  <span className="badge bg-amber-400/15 text-amber-300">
                    wrote back
                  </span>
                )}
                {email.matched_profile_name && (
                  <span className="badge bg-white/10 text-white/60">
                    {email.matched_profile_name}
                  </span>
                )}
              </div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * One detected message: what they wrote, what we made of it, and the reply.
 *
 * The reply panel is the same `DraftReply` the Conversations tab uses, hitting
 * the same review endpoints. That is the point — a draft has one state and one
 * set of controls however the user reached it.
 */
function RecruiterEmailDetail({ emailId, onBack, onChange, onError }) {
  const [email, setEmail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const load = useCallback(async () => {
    if (emailId == null) return;
    setLoading(true);
    try {
      const detail = await api.recruiterEmail(emailId);
      setEmail(detail);
      if (!detail.read_at) {
        await api.markRecruiterEmailRead(emailId);
        await onChange();
      }
    } catch (err) {
      onError(err.message);
    } finally {
      setLoading(false);
    }
  }, [emailId, onChange, onError]);

  useEffect(() => {
    load();
  }, [load]);

  async function run(fn, successMessage) {
    onError(null);
    setBusy(true);
    try {
      await fn();
      toast.success(successMessage);
      await load();
      await onChange();
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (emailId == null) {
    return (
      <div className="panel hidden items-center justify-center px-6 py-20 text-center text-sm text-white/30 lg:flex">
        Select a message to read it.
      </div>
    );
  }

  if (!email) return loading ? <SkeletonLoader rows={[320]} /> : null;

  const [kindLabel, kindTone] =
    RECRUITER_KIND_STYLE[email.kind] ?? RECRUITER_KIND_STYLE.UNKNOWN;
  const routeStyle = email.route ? REPLY_ROUTE_STYLE[email.route] : null;
  const role = email.extracted?.role_title;
  const company = email.extracted?.company;

  return (
    <div className="space-y-4">
      <section className="panel overflow-hidden">
        <header className="space-y-2 border-b px-5 py-4 hairline">
          <button className="btn-quiet -ml-2 lg:hidden" onClick={onBack}>
            ← All messages
          </button>
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <p className="truncate font-display text-xl tracking-tight text-white">
                {email.from_name || email.from_address}
              </p>
              <p className="mt-0.5 truncate text-xs text-white/40">
                <span className="font-mono">{email.from_address}</span>
              </p>
            </div>
            <span className={`badge shrink-0 ${kindTone}`}>{kindLabel}</span>
          </div>
          {email.subject && (
            <p className="truncate text-sm text-white/60">{email.subject}</p>
          )}
          {(role || company) && (
            <p className="text-xs text-white/45">
              {[role, company].filter(Boolean).join(" · ")}
            </p>
          )}
        </header>

        {/* Above the message, not below it: the fact that this is a second
            approach changes how the first line reads. */}
        {email.escalated && email.escalation_reason && (
          <p className="border-b bg-amber-400/[0.06] px-5 py-3 text-sm leading-relaxed text-amber-200/90 hairline">
            {email.escalation_reason}
          </p>
        )}

        <div className="px-5 py-5">
          <p className="eyebrow mb-2">They wrote</p>
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-white/65">
            {email.body_text}
          </p>
          {/* Often the covering note is the short part and the spec attached to
              it is the thing worth reading. `attachment_email_id` is null until
              the conversation exists, and `Attachments` falls back to naming
              the file rather than offering a button that can only fail. */}
          <Attachments
            files={email.attachments}
            emailId={email.attachment_email_id}
            className="mt-3"
          />
        </div>
      </section>

      <MatchPanel
        email={email}
        routeStyle={routeStyle}
        busy={busy}
        onRematch={(profileId) =>
          run(
            () => api.rematchRecruiterEmail(email.id, { profile_id: profileId }),
            "Rematched.",
          )
        }
        onGenerate={() =>
          run(() => api.generateRecruiterReply(email.id), "Reply drafted.")
        }
        onDismiss={() =>
          run(() => api.dismissRecruiterEmail(email.id), "Dismissed.")
        }
      />

      {email.reply_email_id != null && email.reply_status === "DRAFT" && (
        <section className="panel overflow-hidden">
          <DraftReply
            draft={{
              id: email.reply_email_id,
              subject: email.reply_subject,
              body_text: email.reply_body,
              draft_note: email.reply_note,
              draft_template: null,
              attachments: email.reply_attachments,
              attachment_note: email.reply_attachment_note,
            }}
            // Where the answer actually goes. Differs from the From when the
            // sender's platform mails from an unmonitored address, and the
            // confirmation dialog must name the address that will receive it.
            to={email.reply_to_address || email.from_address}
            replyNonce={0}
            onChange={async () => {
              await load();
              await onChange();
            }}
            onError={onError}
          />
        </section>
      )}

      {email.reply_email_id != null && email.reply_status !== "DRAFT" && (
        <section className="panel space-y-3 p-5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="eyebrow">Your reply</span>
            <span className="badge bg-sky-400/15 text-sky-300">
              {email.reply_status === "SENT" ? "sent" : email.reply_status.toLowerCase()}
            </span>
            <span className="font-mono text-[11px] text-white/30">
              → {email.reply_to_address || email.from_address}
            </span>
          </div>
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-white/70">
            {email.reply_body}
          </p>
          <Attachments
            files={email.reply_attachments}
            emailId={email.reply_email_id}
            className=""
          />
        </section>
      )}
    </div>
  );
}

/**
 * Which profile matched, how well, and what that meant. The score and the band
 * are shown together deliberately: "78, so we drafted it" is a sentence the user
 * can argue with, and the profile picker is how they argue.
 */
function MatchPanel({ email, routeStyle, busy, onRematch, onGenerate, onDismiss }) {
  const [profiles, setProfiles] = useState([]);

  useEffect(() => {
    api
      .listProfiles()
      .then((rows) => setProfiles(rows ?? []))
      .catch(() => {});
  }, []);

  return (
    <section className="panel space-y-4 p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="eyebrow">What we made of it</p>
        <div className="flex flex-wrap items-center gap-2">
          {routeStyle && (
            <span className={`badge ${routeStyle[1]}`}>{routeStyle[0]}</span>
          )}
          {email.route_confidence != null && email.route != null && (
            <span className="font-mono text-[10px] text-white/35">
              {Math.round(email.route_confidence)}% confident
            </span>
          )}
        </div>
      </div>

      {/* An escalated message is flagged *because* the recruiter wrote back, so
          its flag_reason is the same sentence the banner above already carries.
          Printing both puts one sentence on the screen twice — fall through to
          the match reason, which is the thing this panel is actually for. */}
      {(() => {
        const flag =
          email.flag_reason === email.escalation_reason ? null : email.flag_reason;
        const reason = flag || email.match_reason;
        return reason ? (
          <p className="text-sm leading-relaxed text-white/60">{reason}</p>
        ) : null;
      })()}

      {/* Why this reply is carrying the document it is carrying. Shown because
          silently swapping a candidate's resume is the kind of helpfulness that
          reads as a bug when they notice it after the fact. */}
      {email.resume_choice_reason && (
        <p className="text-xs leading-relaxed text-white/40">
          {email.resume_choice_reason}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <label className="text-xs text-white/40" htmlFor={`profile-${email.id}`}>
          Matched profile
        </label>
        <select
          id={`profile-${email.id}`}
          className="input max-w-xs"
          value={email.matched_profile_id ?? ""}
          disabled={busy || profiles.length === 0}
          onChange={(e) =>
            onRematch(e.target.value ? Number(e.target.value) : null)
          }
        >
          <option value="">No profile matched</option>
          {profiles.map((profile) => (
            <option key={profile.id} value={profile.id}>
              {profile.name}
            </option>
          ))}
        </select>
        {email.match_score != null && (
          <span className="font-mono text-xs text-white/50">
            {Math.round(email.match_score)}/100
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t pt-4 hairline">
        {email.reply_email_id == null &&
          ["RECRUITER_OUTREACH", "HIRING_MANAGER"].includes(email.kind) && (
            <button className="btn-primary" onClick={onGenerate} disabled={busy}>
              {busy ? "Working…" : "Write a reply"}
            </button>
          )}
        <button
          className="btn-quiet ml-auto hover:text-bad"
          onClick={onDismiss}
          disabled={busy}
        >
          Dismiss
        </button>
      </div>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Drafts                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * Every draft awaiting approval, in one column — the old Review page, now a
 * view of the inbox rather than a destination of its own. Reply drafts appear
 * here *and* inline on their conversation; outreach drafts (from a campaign run
 * in review mode) have no conversation yet, so this is the only place they can
 * be seen.
 */
function DraftsView({ onTab }) {
  const [queue, setQueue] = useState(null);
  const [threadCount, setThreadCount] = useState(0);
  const [recruiterCount, setRecruiterCount] = useState(0);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    const data = await api.review();
    setQueue(data);
    notifyCountsChanged();
  }, []);

  useEffect(() => {
    refresh().catch((err) => setError(err.message));
  }, [refresh]);

  useEffect(() => {
    api
      .inbox()
      .then((data) => setThreadCount(data?.counts?.threads ?? 0))
      .catch(() => {});
    api
      .recruiterInbox()
      .then((inbox) => setRecruiterCount(inbox?.counts?.needs_you ?? 0))
      .catch(() => {});
  }, [queue]);

  if (!queue && error)
    return (
      <div className="space-y-4">
        <ErrorBanner onDismiss={() => setError(null)}>{error}</ErrorBanner>
        <button
          className="btn-ghost"
          onClick={() => refresh().catch((err) => setError(err.message))}
        >
          Try again
        </button>
      </div>
    );
  if (!queue) return <SkeletonLoader rows={[96, 192]} />;

  return (
    <div className="space-y-8">
      <header className="animate-fade-up">
        <p className="eyebrow">Inbox</p>
        <h1 className="mt-3 font-display text-4xl leading-none tracking-tightest text-white">
          {queue.count === 0 ? "Nothing waiting." : `${queue.count} to review`}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-white/45">
          {queue.count === 0
            ? "When a recruiter replies, or you run in review mode, drafts land here for your approval."
            : "Read each draft, tweak it if you like, then send or discard. Approved messages go out from your own inbox."}
        </p>
      </header>

      <ErrorBanner onDismiss={() => setError(null)}>{error}</ErrorBanner>

      <ViewTabs
        tab="drafts"
        onTab={onTab}
        counts={{
          threads: threadCount,
          drafts: queue.count,
          recruiters: recruiterCount,
        }}
      />

      <div className="space-y-3">
        {queue.items.map((item) => (
          <DraftCard
            key={item.email_id}
            item={item}
            onChange={refresh}
            onError={setError}
          />
        ))}
      </div>
    </div>
  );
}

function DraftCard({ item, onChange, onError }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [subject, setSubject] = useState(item.subject || "");
  const [body, setBody] = useState(item.body_text || "");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);

  const dirty = subject !== (item.subject || "") || body !== (item.body_text || "");

  async function save() {
    onError(null);
    setBusy(true);
    try {
      await api.editEmail(item.email_id, { subject, body_text: body });
      setEditing(false);
      toast.success("Draft saved.");
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (
      !(await confirm({
        title: "Send this message?",
        message: `It will go out from your inbox to ${item.to_address}. This can't be recalled.`,
        confirmLabel: "Approve & send",
      }))
    )
      return;
    await run(api.approveDraft, "Sent.");
  }

  async function discard() {
    if (
      !(await confirm({
        title: "Discard this draft?",
        message: "The draft will be deleted and never sent. This can't be undone.",
        confirmLabel: "Discard draft",
        tone: "danger",
      }))
    )
      return;
    await run(api.dismissDraft, "Draft discarded.");
  }

  async function run(fn, successMessage) {
    onError(null);
    setBusy(true);
    try {
      if (dirty) await api.editEmail(item.email_id, { subject, body_text: body });
      await fn(item.email_id);
      toast.success(successMessage);
      await onChange();
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      // A failed approve used to leave the button reading "Working…" forever,
      // so the only way to retry was a reload.
      setBusy(false);
    }
  }

  return (
    <article className="panel overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3 hairline">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={[
              "badge",
              item.kind === "reply"
                ? "bg-signal/15 text-signal"
                : "bg-white/10 text-white/60",
            ].join(" ")}
          >
            {item.kind}
          </span>
          {item.reply_intent && (
            <span className="badge bg-white/5 text-white/45">
              {item.reply_intent.toLowerCase().replace("_", " ")}
            </span>
          )}
          <span className="font-mono text-xs text-white/60">
            {item.company || item.recruiter_name || item.to_address}
          </span>
          {item.role && <span className="text-xs text-white/35">· {item.role}</span>}
        </div>
        <span className="font-mono text-[11px] text-white/30">→ {item.to_address}</span>
      </div>

      {item.incoming_snippet && (
        <div className="border-b bg-white/[0.02] px-5 py-3 hairline">
          <p className="eyebrow mb-1">They wrote</p>
          <p className="text-sm text-white/55">{item.incoming_snippet}</p>
        </div>
      )}

      {/* The same controls the conversation view offers. A draft appears in
          three places and should behave the same in all of them — a user who
          can swap the resume from one screen and not another has to remember
          which screen. */}
      <DraftAttachments
        emailId={item.email_id}
        initialFiles={item.attachments}
        note={item.attachment_note}
        onError={onError}
      />

      <div className="space-y-3 px-5 py-4">
        {editing ? (
          <>
            <input
              className="input font-medium"
              aria-label="Draft subject"
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="Subject"
            />
            <textarea
              className="input min-h-[180px] font-mono text-[13px] leading-relaxed"
              aria-label="Draft body"
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
          </>
        ) : (
          <>
            <p className="text-sm font-medium text-white">{subject}</p>
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-white/70">{body}</p>
          </>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t px-5 py-3 hairline">
        <button className="btn-primary" onClick={approve} disabled={busy}>
          {busy ? "Working…" : "Approve & send"}
        </button>
        {editing ? (
          <button className="btn-ghost" onClick={save} disabled={busy || !dirty}>
            Save edits
          </button>
        ) : (
          <button className="btn-ghost" onClick={() => setEditing(true)} disabled={busy}>
            Edit
          </button>
        )}
        <button className="btn-quiet ml-auto hover:text-bad" onClick={discard} disabled={busy}>
          Discard
        </button>
      </div>
    </article>
  );
}

/**
 * Nothing to show. Which is a different problem depending on why: filters that
 * are too narrow are the user's to widen, but a genuinely empty mailbox means
 * nothing has been sent yet — and the fix for that is autopilot, not this page.
 */
function EmptyState({ filtered, onClear, onSync, syncing }) {
  return (
    <div className="panel px-6 py-16 text-center">
      <p className="font-display text-2xl tracking-tight text-white/80">
        {filtered ? "Nothing matches those filters" : "No email yet"}
      </p>
      <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-white/40">
        {filtered
          ? "Widen the filters to see the rest of your mail."
          : "Every application sent on your behalf shows up here, alongside the replies. Start the autopilot — or approve a draft — and the first one lands within minutes."}
      </p>
      {filtered ? (
        <button className="btn-ghost mt-6" onClick={onClear}>
          Clear filters
        </button>
      ) : (
        <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
          {/* Autopilot is switched on from the pipeline header, which is where
              the first send will come from. */}
          <Link className="btn-primary" to="/pipeline">
            Start the autopilot
          </Link>
          <button className="btn-ghost" onClick={onSync} disabled={syncing}>
            {syncing ? "Checking…" : "Check for new mail"}
          </button>
        </div>
      )}
    </div>
  );
}
