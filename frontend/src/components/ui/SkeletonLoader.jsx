/**
 * The one loading placeholder for the whole app. Every page was rolling its own
 * stack of pulsing rectangles; this collapses them into one shape.
 *
 *   <SkeletonLoader rows={[24, 56, 64]} />   // three blocks of those heights
 *   <SkeletonLoader count={3} height={96} shimmer />
 *
 * `shimmer` uses the sweeping highlight (as the setup wizard did); the default
 * is the cheaper opacity pulse.
 */
export default function SkeletonLoader({
  rows,
  count = 3,
  height = 96,
  shimmer = false,
  className = "",
}) {
  const heights = rows ?? Array.from({ length: count }, () => height);

  return (
    <div className={["space-y-3", className].join(" ")} data-testid="skeleton-loader">
      {heights.map((h, i) => (
        <Block key={i} height={h} shimmer={shimmer} />
      ))}
    </div>
  );
}

function Block({ height, shimmer }) {
  if (shimmer) {
    return (
      <div
        className="relative overflow-hidden rounded-xl border bg-ink-800/60 hairline"
        style={{ height }}
      >
        <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-white/[0.04] to-transparent" />
      </div>
    );
  }
  return (
    <div
      className="animate-pulse rounded-xl border bg-ink-800/60 hairline"
      style={{ height }}
    />
  );
}
