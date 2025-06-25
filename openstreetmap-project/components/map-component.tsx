"use client"

import { useEffect, useRef, useState } from "react"
import L from "leaflet"
import "leaflet/dist/leaflet.css"
import { WindDirectionLayer } from "./wind-direction-layer"

// Leaflet 아이콘 문제 해결
delete (L.Icon.Default.prototype as any)._getIconUrl
L.Icon.Default.mergeOptions({
  iconRetinaUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon-2x.png",
  iconUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon.png",
  shadowUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-shadow.png",
})

interface WindData {
  timestamp: string
  variable: string
  bbox: [number, number, number, number]
  resolution: [number, number]
  data: Array<{
    lat: number
    lon: number
    value: number
  }>
}

export default function MapComponent() {
  const mapRef = useRef<HTMLDivElement>(null)
  const mapInstanceRef = useRef<L.Map | null>(null)
  const windLayerRef = useRef<WindDirectionLayer | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [windData, setWindData] = useState<WindData | null>(null)
  const today = new Date().toISOString().split("T")[0] // "yyyy-mm-dd"
  const [selectedDate, setSelectedDate] = useState(today)

  useEffect(() => {
    if (!mapRef.current || mapInstanceRef.current) return

    // 지도 초기화 (API 데이터 영역 중심으로 설정: 128.3-129.8E, 34.35-35.85N)
    const map = L.map(mapRef.current, {
      center: [35.1, 129.05], // 데이터 영역 중심
      zoom: 9,
      zoomControl: true,
      attributionControl: true,
    })

    // OpenStreetMap 타일 레이어 추가
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    }).addTo(map)

    mapInstanceRef.current = map

    // 컴포넌트 언마운트 시 지도 정리
    return () => {
      if (mapInstanceRef.current) {
        mapInstanceRef.current.remove()
        mapInstanceRef.current = null
      }
    }
  }, [])

  const formatTimestamp = (isoString: string) => {
    // 백엔드에서 받은 "20250617T06:00:00Z" 를 JS Date로 바꿔서 한국 시간으로 표시
    try {
      const date = new Date(isoString)
      return date.toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })
    } catch {
      return isoString
    }
  }


  const loadWindData = async () => {
    if (!mapInstanceRef.current) return

    setIsLoading(true)
    try {
      console.log(selectedDate, "!!")
      const response = await fetch(`http://bluemap.kr:21809/api/gfs/wind-direction?date=${selectedDate}`)

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`)
      }

      const data: WindData[] = await response.json()

      if (data.length === 0) {
        throw new Error("No wind data available for the selected date.")
      }

      const windDirectionData = data[0] // 첫 번째 데이터 사용
      setWindData(windDirectionData)

      // 기존 바람 레이어 제거
      if (windLayerRef.current) {
        windLayerRef.current.remove()
      }

      // 새 바람 방향 레이어 추가
      windLayerRef.current = new WindDirectionLayer(windDirectionData)
      windLayerRef.current.addTo(mapInstanceRef.current)

      // 지도를 데이터 영역으로 이동
      const [west, south, east, north] = windDirectionData.bbox
      mapInstanceRef.current.fitBounds(
        [
          [south, west],
          [north, east],
        ],
        { padding: [20, 20] },
      )
    } catch (error) {
      console.error("Failed to load wind data:", error)
      alert(`Failed to load wind data: ${error}`)
    } finally {
      setIsLoading(false)
    }
  }

  const toggleWindLayer = () => {
    if (!windLayerRef.current || !mapInstanceRef.current) return

    if (mapInstanceRef.current.hasLayer(windLayerRef.current as any)) {
      mapInstanceRef.current.removeLayer(windLayerRef.current as any)
    } else {
      windLayerRef.current.addTo(mapInstanceRef.current)
    }
  }

  const getValidDataCount = () => {
    if (!windData) return 0
    return windData.data.filter((point) => point.value < 999900000000000000000).length
  }

  return (
    <div className="relative h-full w-full">
      <div ref={mapRef} className="h-full w-full" style={{ zIndex: 0 }} />

      {/* 상단 컨트롤 패널 */}
      <div className="absolute top-4 left-4 z-[1000] bg-white rounded-lg shadow-lg p-4 min-w-[250px]">
        <h2 className="text-lg font-semibold mb-3">GFS Wind Direction Data Collection Viewer</h2>

        <div>
          <p className="italic text-gray-500 mb-3">This demo visualizes GFS wind direction data collected near Busan Port.</p>
        </div>

        <div className="space-y-3">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Select Date</label>
            <input
              type="date"
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm"
            />
          </div>

          <button
            className={`w-full px-3 py-2 text-white rounded transition-colors ${
              isLoading ? "bg-gray-400 cursor-not-allowed" : "bg-blue-500 hover:bg-blue-600"
            }`}
            onClick={loadWindData}
            disabled={isLoading}
          >
            {isLoading ? "Loading..." : "Load Wind Data"}
          </button>

          {windData && (
            <button
              className="w-full px-3 py-2 bg-green-500 text-white rounded hover:bg-green-600 transition-colors"
              onClick={toggleWindLayer}
            >
              Toggle Wind Direction Layer
            </button>
          )}
        </div>

        {windData && (
          <div className="mt-3 pt-3 border-t border-gray-200">
            <div className="text-xs text-gray-600 space-y-1">
              <div>Timestamp: {new Date(windData.timestamp).toLocaleString()}</div>
              <div>Valid Data: {getValidDataCount()}</div>
              <div>Total Grids: {windData.data.length}</div>
              <div>
                Resolution: {windData.resolution[0]}° × {windData.resolution[1]}°
              </div>
              <div>
                Area: {windData.bbox[0]}°-{windData.bbox[2]}°E, {windData.bbox[1]}°-{windData.bbox[3]}°N
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 범례 */}
      {windData && (
        <div className="absolute top-4 right-4 z-[1000] bg-white rounded-lg shadow-lg p-3">
          <h3 className="text-sm font-semibold mb-2">Wind Direction</h3>
          <div className="space-y-2">
            <div className="flex items-center space-x-2">
              <div className="w-4 h-2 bg-blue-500"></div>
              <span className="text-xs">North Wind (0°-90°)</span>
            </div>
            <div className="flex items-center space-x-2">
              <div className="w-4 h-2 bg-green-500"></div>
              <span className="text-xs">East Wind (90°-180°)</span>
            </div>
            <div className="flex items-center space-x-2">
              <div className="w-4 h-2 bg-yellow-500"></div>
              <span className="text-xs">South Wind (180°-270°)</span>
            </div>
            <div className="flex items-center space-x-2">
              <div className="w-4 h-2 bg-red-500"></div>
              <span className="text-xs">West Wind (270°-360°)</span>
            </div>
            <div className="flex items-center space-x-2">
              <div className="w-4 h-2 bg-gray-400"></div>
              <span className="text-xs">Missing Data</span>
            </div>
          </div>
        </div>
      )}

      {/* 우측 하단 정보 패널 */}
      <div className="absolute bottom-4 right-4 z-[1000] bg-white rounded-lg shadow-lg p-3">
        <div className="text-sm text-gray-600">
          <div>NOAA GFS API</div>
          <div>Wind Direction Data</div>
          <div>Using Leaflet.js</div>
          <div>© 2025 BLUEMAP</div>
        </div>
      </div>
    </div>
  )
}
