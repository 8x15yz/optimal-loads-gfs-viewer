"use client"

import L from "leaflet"

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

export class WindDirectionLayer extends L.Layer {
  private windData: WindData
  private canvas: HTMLCanvasElement | null = null
  private ctx: CanvasRenderingContext2D | null = null

  constructor(windData: WindData) {
    super()
    this.windData = windData
  }

  onAdd(map: L.Map): this {
    this.canvas = L.DomUtil.create("canvas", "wind-direction-layer-canvas")
    this.ctx = this.canvas.getContext("2d")

    const size = map.getSize()
    this.canvas.width = size.x
    this.canvas.height = size.y
    this.canvas.style.position = "absolute"
    this.canvas.style.top = "0"
    this.canvas.style.left = "0"
    this.canvas.style.pointerEvents = "none"
    this.canvas.style.zIndex = "200"

    map.getPanes().overlayPane?.appendChild(this.canvas)

    this.draw(map)

    // 지도 이벤트 리스너
    map.on("zoom", () => this.draw(map))
    map.on("move", () => this.draw(map))

    return this
  }

  onRemove(map: L.Map): this {
    if (this.canvas && this.canvas.parentNode) {
      this.canvas.parentNode.removeChild(this.canvas)
    }

    map.off("zoom")
    map.off("move")

    return this
  }

  private draw(map: L.Map) {
    if (!this.ctx || !this.canvas) return

    const size = map.getSize()
    this.canvas.width = size.x
    this.canvas.height = size.y

    // 캔버스 클리어
    this.ctx.clearRect(0, 0, size.x, size.y)

    // 바람 방향 데이터 그리기
    this.windData.data.forEach((point) => {
      // 결측값 체크
      if (point.value >= 999900000000000000000) {
        return // 결측값은 그리지 않음
      }

      const pixelPoint = map.latLngToContainerPoint([point.lat, point.lon])

      // 화면 범위 내에 있는 점만 그리기
      if (pixelPoint.x >= 0 && pixelPoint.x <= size.x && pixelPoint.y >= 0 && pixelPoint.y <= size.y) {
        this.drawWindDirection(pixelPoint.x, pixelPoint.y, point.value)
      }
    })
  }

  private drawWindDirection(x: number, y: number, windDirection: number) {
    if (!this.ctx) return

    // 바람 방향에 따른 색상 결정
    const color = this.getDirectionColor(windDirection)

    // 바람 방향 화살표 그리기
    const arrowLength = 15
    // 기상학적 바람 방향: 바람이 불어오는 방향 (180도 회전 필요)
    const angle = ((windDirection + 180) * Math.PI) / 180

    this.ctx.strokeStyle = color
    this.ctx.fillStyle = color
    this.ctx.lineWidth = 2

    // 화살표 몸체
    this.ctx.beginPath()
    this.ctx.moveTo(x, y)
    this.ctx.lineTo(x + arrowLength * Math.cos(angle), y + arrowLength * Math.sin(angle))
    this.ctx.stroke()

    // 화살표 머리
    const headLength = 5
    const headAngle = Math.PI / 6

    this.ctx.beginPath()
    this.ctx.moveTo(x + arrowLength * Math.cos(angle), y + arrowLength * Math.sin(angle))
    this.ctx.lineTo(
      x + (arrowLength - headLength) * Math.cos(angle - headAngle),
      y + (arrowLength - headLength) * Math.sin(angle - headAngle),
    )
    this.ctx.moveTo(x + arrowLength * Math.cos(angle), y + arrowLength * Math.sin(angle))
    this.ctx.lineTo(
      x + (arrowLength - headLength) * Math.cos(angle + headAngle),
      y + (arrowLength - headLength) * Math.sin(angle + headAngle),
    )
    this.ctx.stroke()

    // 격자점 표시 (작은 원)
    this.ctx.beginPath()
    this.ctx.arc(x, y, 2, 0, 2 * Math.PI)
    this.ctx.fill()
  }

  private getDirectionColor(direction: number): string {
    // 바람 방향을 0-360도 범위로 정규화
    const normalizedDirection = ((direction % 360) + 360) % 360

    if (normalizedDirection >= 0 && normalizedDirection < 90) {
      return "#3B82F6" // 북풍 - 파란색
    } else if (normalizedDirection >= 90 && normalizedDirection < 180) {
      return "#10B981" // 동풍 - 초록색
    } else if (normalizedDirection >= 180 && normalizedDirection < 270) {
      return "#F59E0B" // 남풍 - 노란색
    } else {
      return "#EF4444" // 서풍 - 빨간색
    }
  }
}
