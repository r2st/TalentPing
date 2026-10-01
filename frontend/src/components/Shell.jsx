import { useEffect, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import Logo from "./ui/Logo";
import { api } from "../lib/api";
import { onCountsChanged } from "../lib/events";

// Four destinations, in the order the work happens: watch what's in flight,
// pick what to go after, read what came back, and change how any of it runs.
//
// Setup used to leave the nav the moment onboarding finished, replaced by an
// unlabelled gear — which meant the one page holding every resume and every
// search preference had no named route to it for the entire life of an account,
// and none at all whenever the onboarding read failed (the hook assumed
// "finished" on error). It is a destination like the rest now.
const TABS = [
  { to: "/pipeline", label: "Pipeline" },
  { to: "/jobs", label: "Jobs" },
  { to: "/inbox", label: "Inbox" },
  { to: "/setup", label: "Setup" },
];

/**
 * The only chrome in the app: a wordmark, the destinations, and the account.
 * On a phone the destinations collapse into a menu; on wider screens they sit
 * inline. The Inbox tab carries a count of everything waiting on the user —
 * unread recruiter replies plus drafts needing approval, which now live on the
 * same page.
 */
export default function Shell({ children }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const counts = useNavCounts(location.pathname);
  const tabs = TABS;

  // Close the mobile menu on navigation.
  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b bg-ink-900/80 backdrop-blur-xl hairline">
        <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-5">
          <div className="flex min-w-0 items-center gap-5 sm:gap-8">
            <Wordmark />
            {/* Inline nav from the small breakpoint up; a scroll strip keeps it
                from wrapping the header if it ever overflows. */}
            <nav className="hidden items-center gap-1 overflow-x-auto sm:flex">
              {tabs.map((tab) => (
                <Tab key={tab.to} to={tab.to} end={tab.end} badge={badgeFor(tab, counts)}>
                  {tab.label}
                </Tab>
              ))}
            </nav>
          </div>

          <div className="flex shrink-0 items-center gap-3">
            <span className="hidden font-mono text-xs text-white/35 lg:block">
              {user?.email}
            </span>
            <button
              className="btn-quiet hidden sm:inline-flex"
              onClick={() => {
                logout();
                navigate("/login");
              }}
            >
              Sign out
            </button>

            {/* Hamburger — phones only. */}
            <button
              className="btn-quiet -mr-1 inline-flex sm:hidden"
              onClick={() => setMenuOpen((v) => !v)}
              aria-expanded={menuOpen}
              aria-label="Toggle navigation menu"
            >
              <MenuGlyph open={menuOpen} />
              {counts.review + counts.inbox > 0 && !menuOpen && (
                <span className="ml-1 h-1.5 w-1.5 rounded-full bg-signal" aria-hidden="true" />
              )}
            </button>
          </div>
        </div>

        {menuOpen && (
          <nav className="border-t px-3 py-2 hairline sm:hidden">
            {tabs.map((tab) => (
              <MobileTab key={tab.to} to={tab.to} end={tab.end} badge={badgeFor(tab, counts)}>
                {tab.label}
              </MobileTab>
            ))}
            <button
              className="mt-1 block w-full rounded-md px-3 py-2.5 text-left text-sm text-white/40 hover:bg-white/[0.04] hover:text-white/75"
              onClick={() => {
                logout();
                navigate("/login");
              }}
            >
              Sign out
            </button>
          </nav>
        )}
      </header>

      <main className="mx-auto max-w-5xl px-5 py-10">{children}</main>
    </div>
  );
}

/** Poll the two queues the tab badges show — drafts awaiting approval, and
 *  unread recruiter replies. Both now live on the Inbox, so they add up into
 *  one badge. Refreshed when the route changes so acting on a draft updates the
 *  count on the way out. */
function useNavCounts(pathname) {
  const [counts, setCounts] = useState({ review: 0, inbox: 0 });

  useEffect(() => {
    let cancelled = false;
    const fetchCounts = () =>
      Promise.all([
        api.review().catch(() => null),
        api.inbox().catch(() => null),
      ]).then(([queue, inbox]) => {
        if (cancelled) return;
        setCounts({
          review: queue?.count ?? 0,
          inbox: inbox?.counts?.unread ?? 0,
        });
      });
    fetchCounts();
    const timer = setInterval(fetchCounts, 30000);
    // Reading a reply or approving a draft changes a count now, not in 30s.
    const unsubscribe = onCountsChanged(fetchCounts);
    return () => {
      cancelled = true;
      clearInterval(timer);
      unsubscribe();
    };
  }, [pathname]);

  return counts;
}

/**
 * The Inbox badge counts everything waiting on the user: unread replies plus
 * drafts needing approval. They were two tabs and two badges; they are one page
 * now, so a single number is the honest summary of "how much is on you".
 */
function badgeFor(tab, counts) {
  if (tab.to !== "/inbox") return null;
  const total = counts.inbox + counts.review;
  return total > 0 ? { value: total, label: "waiting on you" } : null;
}

function Wordmark() {
  return (
    <span className="flex shrink-0 items-center gap-2.5">
      <Logo className="h-[26px] w-[26px] shrink-0 text-signal" title="DoAide AutoApply" />
      <span className="font-display text-xl tracking-tight text-white">
        DoAide<span className="italic text-signal"> AutoApply</span>
      </span>
    </span>
  );
}

function Tab({ to, end, badge, children }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        [
          "relative shrink-0 whitespace-nowrap rounded-md py-1.5 text-sm",
          // A badged tab reserves space for its own count, so it can't sit on
          // top of the next label — two adjacent badges made that visible.
          badge ? "pl-3 pr-7" : "px-3",
          "transition-colors",
          isActive ? "text-white" : "text-white/40 hover:text-white/75",
        ].join(" ")
      }
    >
      {({ isActive }) => (
        <>
          {children}
          {badge && <CountBadge {...badge} />}
          {isActive && (
            <span className="absolute inset-x-3 -bottom-[13px] h-px bg-signal" />
          )}
        </>
      )}
    </NavLink>
  );
}

function MobileTab({ to, end, badge, children }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        [
          "flex items-center justify-between rounded-md px-3 py-2.5 text-sm transition-colors",
          isActive
            ? "bg-white/[0.06] text-white"
            : "text-white/45 hover:bg-white/[0.04] hover:text-white/80",
        ].join(" ")
      }
    >
      <span>{children}</span>
      {badge && <CountBadge {...badge} inline />}
    </NavLink>
  );
}

function CountBadge({ value, label, inline = false }) {
  return (
    <span
      className={[
        inline ? "" : "absolute right-1 top-1/2 -translate-y-1/2",
        "inline-flex min-w-[1.15rem] items-center justify-center rounded-full bg-signal px-1",
        "font-mono text-[10px] font-semibold leading-[1.15rem] text-ink-900",
      ].join(" ")}
      aria-label={`${value} ${label}`}
    >
      {value > 9 ? "9+" : value}
    </span>
  );
}

function MenuGlyph({ open }) {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      {open ? (
        <>
          <path d="M5 5l10 10" />
          <path d="M15 5L5 15" />
        </>
      ) : (
        <>
          <path d="M3 6h14" />
          <path d="M3 10h14" />
          <path d="M3 14h14" />
        </>
      )}
    </svg>
  );
}
