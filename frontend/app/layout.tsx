import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Audio Notes",
  description: "Upload audio, get a transcript (Gnani ASR) and a summary (Gemini).",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <nav className="nav">
          <div className="nav-inner">
            <Link href="/" className="brand">🎙️ Audio Notes</Link>
            <Link href="/">Upload</Link>
            <Link href="/architecture">Architecture</Link>
          </div>
        </nav>
        <main>{children}</main>
      </body>
    </html>
  );
}
