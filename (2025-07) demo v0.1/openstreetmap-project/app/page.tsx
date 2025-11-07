"use client"

import dynamic from "next/dynamic"

// Leaflet을 동적으로 로드 (SSR 방지)
const MapComponent = dynamic(() => import("@/components/map-component"), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-screen bg-gray-100">
      <div className="text-lg">지도를 로딩중입니다...</div>
    </div>
  ),
})

export default function HomePage() {
  return (
    <main className="h-screen w-full">
      <MapComponent />
    </main>
  )
}
