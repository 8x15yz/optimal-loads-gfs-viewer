import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'GFS Viewer',
  description: 'Created with bluemap',
  generator: 'bluemap',
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}
