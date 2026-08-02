import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

// Self-hosted at build time by next/font, so no runtime request to a font CDN
// and no layout shift. Geist is OFL-licensed, satisfying §23.3's requirement
// that dependency licences suit a public repository.
const geist = Geist({ subsets: ["latin"], variable: "--font-geist", display: "swap" });
const geistMono = Geist_Mono({
  subsets: ["latin"],
  variable: "--font-geist-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "TAKEGRAPH — Change one detail. Rebuild only what changed.",
  description:
    "TAKEGRAPH keeps multimodel media productions consistent, recoverable and verifiable. " +
    "It knows what produced every output, preserves valid work, and rebuilds only what a change invalidated.",
  openGraph: {
    title: "TAKEGRAPH",
    description: "The self-healing build system for generative media.",
    type: "website",
  },
};

export const viewport: Viewport = {
  themeColor: "#050608",
  colorScheme: "dark",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${geist.variable} ${geistMono.variable}`}>
      <body className="bg-canvas text-ink font-sans antialiased">
        {/* §18.14: keyboard users reach content without traversing the whole nav. */}
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-[100] focus:rounded-none focus:border focus:border-signal focus:bg-canvas focus:px-4 focus:py-2 focus:text-sm"
        >
          Skip to content
        </a>
        {children}
      </body>
    </html>
  );
}
