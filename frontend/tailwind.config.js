/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      fontFamily: {
        // Editorial serif for numerals and the wordmark; grotesk for UI; mono
        // for anything that is data (addresses, statuses, counts).
        display: ['"Instrument Serif"', "Georgia", "serif"],
        sans: ['"Schibsted Grotesk"', "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      colors: {
        // Near-black canvas with three elevation steps above it.
        ink: {
          900: "#0a0a0b",
          800: "#101012",
          700: "#16161a",
          600: "#1d1d22",
        },
        // The single accent. TalentPing pings — this is the signal.
        signal: {
          DEFAULT: "#f0b429",
          soft: "#f7d070",
          dim: "#a87c1c",
        },
        good: "#4ade80",
        warn: "#fb923c",
        bad: "#f87171",
      },
      letterSpacing: {
        tightest: "-0.04em",
      },
      boxShadow: {
        lift: "0 1px 0 0 rgba(255,255,255,0.04) inset, 0 8px 24px -12px rgba(0,0,0,0.9)",
        glow: "0 0 0 1px rgba(240,180,41,0.3), 0 0 32px -8px rgba(240,180,41,0.35)",
      },
      keyframes: {
        "fade-up": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "ping-ring": {
          "0%": { transform: "scale(1)", opacity: "0.5" },
          "70%, 100%": { transform: "scale(2.4)", opacity: "0" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.5s cubic-bezier(0.16, 1, 0.3, 1) both",
        "ping-ring": "ping-ring 2.4s cubic-bezier(0, 0, 0.2, 1) infinite",
        shimmer: "shimmer 1.8s infinite",
      },
    },
  },
  plugins: [],
};
