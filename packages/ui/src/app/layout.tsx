import type { Metadata, Viewport } from "next";
import AuthProvider from "@/components/AuthProvider";
import AppShell from "@/components/shell/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Open Executive",
  description: "Your AI-powered virtual executive team",
};

// Mobile viewport contract:
// - `viewportFit: "cover"` lets the layout extend under notches / the home
//   indicator; the shell then pads with env(safe-area-inset-*) where it
//   matters (bottom nav, drawers, composer).
// - `interactiveWidget: "resizes-content"` makes Android Chrome shrink the
//   layout viewport when the on-screen keyboard opens, so the sticky
//   composer (sized with dvh, see globals.css) stays above the keyboard.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  interactiveWidget: "resizes-content",
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#09090b" },
    { media: "(prefers-color-scheme: light)", color: "#fafafa" },
  ],
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="h-full">
      <body className="h-full antialiased bg-surface text-fg">
        <AuthProvider>
          <AppShell>{children}</AppShell>
        </AuthProvider>
      </body>
    </html>
  );
}
