// Small formatting helpers shared across pages.

/**
 * Relative time, both directions: "in 3d" for the future, "2h ago" for the
 * past, and a calendar date once relative time stops being useful. Returns
 * "—" for empty values so table cells stay aligned.
 *
 * Dates outside the current year carry it: the inbox shows a user's whole
 * history, and a bare "Jun 20" on an email from two years ago reads as recent.
 */
export function formatWhen(value) {
  if (!value) return "—";
  const then = new Date(value);
  const minutes = Math.round((then.getTime() - Date.now()) / 60000);
  const future = minutes > 0;
  const abs = Math.abs(minutes);

  if (abs < 1) return "just now";

  let magnitude;
  if (abs < 60) magnitude = `${abs}m`;
  else if (abs < 1440) magnitude = `${Math.round(abs / 60)}h`;
  else if (abs < 10080) magnitude = `${Math.round(abs / 1440)}d`;
  else
    return then.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      ...(then.getFullYear() === new Date().getFullYear()
        ? {}
        : { year: "numeric" }),
    });

  return future ? `in ${magnitude}` : `${magnitude} ago`;
}
