import maplibregl from 'maplibre-gl'
import { Protocol } from 'pmtiles'
import 'maplibre-gl/dist/maplibre-gl.css'

import { getBasemapStyle, type Basemap } from './basemap'
import {
  LAYERS,
  type LayerDef,
  dataLayers,
  defOf,
  legendMarkup,
  opacityProps,
  paletteFor,
  popupHtml,
} from './layers'
import { applyThemeAttr, initialTheme, type Theme } from './theme'
import './style.css'

// PMTiles の場所。dev では vite.config.ts のミドルウェアが build/ を Range 配信し、
// 本番では pages.yml が dist/pmtiles/ に同梱する。どちらも同じ相対パスで引ける。
const PMTILES_URL = new URL('pmtiles/bus_frequency.pmtiles', location.href).href
const DATA_ATTRIBUTION =
  '<a href="https://gtfs-data.jp/" target="_blank" rel="noopener">GTFSデータリポジトリ</a> | ' +
  '<a href="https://ckan.odpt.org/" target="_blank" rel="noopener">公共交通オープンデータセンター</a>'

let theme: Theme = initialTheme()
let base: Basemap = 'pale'
applyThemeAttr(theme)

const isMobile = window.matchMedia('(max-width: 640px)').matches

maplibregl.addProtocol('pmtiles', new Protocol().tile)

function buildStyle(): maplibregl.StyleSpecification {
  const style = structuredClone(getBasemapStyle(base, theme)) as maplibregl.StyleSpecification
  const p = paletteFor(theme, base)
  style.sources = {
    ...style.sources,
    bus: { type: 'vector', url: `pmtiles://${PMTILES_URL}`, attribution: DATA_ATTRIBUTION },
  }
  style.layers = [...style.layers, ...(dataLayers(p) as maplibregl.LayerSpecification[])]
  return style
}

const map = new maplibregl.Map({
  container: 'map',
  style: buildStyle(),
  center: [138.0, 37.0],
  zoom: 5,
  // 地図位置を URL の #ズーム/緯度/経度 に反映（共有・リロード時の位置維持）
  hash: true,
  attributionControl: false,
  // モバイルは GPU/メモリが限られるので保持タイル数と描画バッファを絞る
  maxTileCacheSize: isMobile ? 24 : undefined,
  pixelRatio: isMobile ? Math.min(window.devicePixelRatio || 1, 2) : undefined,
})

map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')
map.addControl(
  new maplibregl.GeolocateControl({
    positionOptions: { enableHighAccuracy: true },
    trackUserLocation: true,
  }),
  'top-right',
)
map.addControl(new maplibregl.ScaleControl({ maxWidth: 120 }), 'bottom-left')
map.addControl(new maplibregl.AttributionControl({ compact: true }))

// 背景スタイルを差し替える。ベクタ（淡色）↔ラスタ（写真）の切替では diff 適用が
// 効かず背景が入れ替わらないため diff:false で完全に再構築する。
// setStyle 直後は isStyleLoaded() が旧スタイルで true を返して競合するため、
// 新スタイルの描画が落ち着く idle を待ってから状態を貼り直す。
function reloadStyle(): void {
  map.setStyle(buildStyle(), { diff: false })
  map.once('idle', applyLayerState)
}

// ---- テーマ切替 ----
const themeBtn = document.getElementById('theme-btn') as HTMLButtonElement
const renderThemeBtn = (): void => {
  themeBtn.textContent = theme === 'dark' ? '☀️' : '🌙'
}
themeBtn.addEventListener('click', () => {
  theme = theme === 'dark' ? 'light' : 'dark'
  applyThemeAttr(theme)
  renderThemeBtn()
  renderLegends()
  reloadStyle()
})

// ---- パネル開閉 ----
const panel = document.getElementById('panel') as HTMLElement
const collapseBtn = document.getElementById('collapse-btn') as HTMLButtonElement
const renderCollapseBtn = (): void => {
  collapseBtn.textContent = panel.classList.contains('collapsed') ? '▾' : '▴'
}
collapseBtn.addEventListener('click', () => {
  panel.classList.toggle('collapsed')
  renderCollapseBtn()
})

// ---- レイヤートグル（凡例は各レイヤーの直下にインライン表示） ----
const layersDiv = document.getElementById('layers') as HTMLElement

function renderLegends(): void {
  const p = paletteFor(theme, base)
  for (const def of LAYERS) {
    const el = layersDiv.querySelector<HTMLElement>(`.layer-item[data-key="${def.key}"] .layer-legend`)
    if (el) el.innerHTML = legendMarkup(def, p)
  }
}

function buildToggles(): void {
  for (const def of LAYERS) {
    const item = document.createElement('div')
    item.className = 'layer-item'
    item.dataset.key = def.key

    const label = document.createElement('label')
    label.className = 'toggle'

    const input = document.createElement('input')
    input.type = 'checkbox'
    input.checked = def.on
    input.addEventListener('change', () => setLayerVisible(def, input.checked))

    const sw = document.createElement('span')
    sw.className = 'switch'
    const text = document.createElement('span')
    text.className = 't-label'
    text.textContent = def.name

    // レイヤーの説明（i ボタンで開閉）
    const desc = document.createElement('div')
    desc.className = 'layer-desc'
    desc.hidden = true
    desc.textContent = def.desc

    const info = document.createElement('button')
    info.type = 'button'
    info.className = 'info-btn'
    info.textContent = 'i'
    info.setAttribute('aria-label', `${def.name}の説明`)
    info.setAttribute('aria-expanded', 'false')
    info.addEventListener('click', (e) => {
      // label 内のボタン。クリックが checkbox のトグルへ波及しないようにする
      e.preventDefault()
      e.stopPropagation()
      const open = desc.hidden
      desc.hidden = !open
      info.setAttribute('aria-expanded', String(open))
    })

    label.append(input, sw, text, info)

    // 不透明度スライダー（有効時のみ表示）
    const opac = document.createElement('div')
    opac.className = 'layer-opacity'
    opac.hidden = !def.on
    const range = document.createElement('input')
    range.type = 'range'
    range.min = '0'
    range.max = '1'
    range.step = '0.05'
    range.value = String(def.opacity)
    range.setAttribute('aria-label', `${def.name}の不透明度`)
    const val = document.createElement('span')
    val.className = 'op-val'
    val.textContent = `${Math.round(def.opacity * 100)}%`
    range.addEventListener('input', () => {
      const v = Number(range.value)
      val.textContent = `${Math.round(v * 100)}%`
      setLayerOpacity(def, v)
    })
    opac.append(range, val)

    const legend = document.createElement('div')
    legend.className = 'layer-legend'
    legend.hidden = !def.on

    item.append(label, desc, opac, legend)
    layersDiv.append(item)
  }
  renderLegends()
}

function setLayerVisible(def: LayerDef, on: boolean): void {
  def.on = on
  if (map.getLayer(def.key)) map.setLayoutProperty(def.key, 'visibility', on ? 'visible' : 'none')
  const item = layersDiv.querySelector<HTMLElement>(`.layer-item[data-key="${def.key}"]`)
  item?.querySelector<HTMLElement>('.layer-legend')?.toggleAttribute('hidden', !on)
  item?.querySelector<HTMLElement>('.layer-opacity')?.toggleAttribute('hidden', !on)
}

function setLayerOpacity(def: LayerDef, v: number): void {
  def.opacity = v
  if (!map.getLayer(def.key)) return
  for (const prop of opacityProps(def.geom)) map.setPaintProperty(def.key, prop, v)
}

/** スタイル差し替え後に、トグル/不透明度の状態を貼り直す。 */
function applyLayerState(): void {
  for (const def of LAYERS) {
    if (!map.getLayer(def.key)) continue
    map.setLayoutProperty(def.key, 'visibility', def.on ? 'visible' : 'none')
    for (const prop of opacityProps(def.geom)) map.setPaintProperty(def.key, prop, def.opacity)
  }
}

// ---- 背景地図スイッチャー（右下） ----
class BasemapControl implements maplibregl.IControl {
  private el!: HTMLElement
  onAdd(): HTMLElement {
    this.el = document.createElement('div')
    this.el.className = 'maplibregl-ctrl basemap-switch'
    const defs: [Basemap, string][] = [
      ['pale', '地図'],
      ['photo', '写真'],
    ]
    for (const [b, label] of defs) {
      const btn = document.createElement('button')
      btn.type = 'button'
      btn.textContent = label
      btn.dataset.base = b
      btn.setAttribute('aria-selected', String(b === base))
      btn.addEventListener('click', () => setBase(b))
      this.el.append(btn)
    }
    return this.el
  }
  onRemove(): void {
    this.el.remove()
  }
  sync(): void {
    for (const btn of this.el.querySelectorAll<HTMLButtonElement>('button')) {
      btn.setAttribute('aria-selected', String(btn.dataset.base === base))
    }
  }
}
const basemapCtrl = new BasemapControl()
map.addControl(basemapCtrl, 'bottom-right')

function setBase(next: Basemap): void {
  if (next === base) return
  base = next
  basemapCtrl.sync()
  renderLegends() // 写真の上では線の明暗の向きが変わる
  reloadStyle()
}

// ---- ポップアップ ----
// 停留所は点なので、線より先に当てる（配列の先頭が優先）。
const HIT_LAYERS = ['stops', 'routes']
function hitFeature(pt: maplibregl.PointLike): maplibregl.MapGeoJSONFeature | null {
  const ids = HIT_LAYERS.filter((id) => map.getLayer(id) && defOf(id)?.on)
  if (!ids.length) return null
  const feats = map.queryRenderedFeatures(pt, { layers: ids })
  if (!feats.length) return null
  // queryRenderedFeatures は描画順で返るので、停留所が含まれていればそれを優先する
  return feats.find((f) => f.layer.id === 'stops') ?? feats[0]
}
const htmlFor = (f: maplibregl.MapGeoJSONFeature): string =>
  popupHtml(f.layer.id, f.properties as Record<string, unknown>)

if (window.matchMedia('(hover: hover)').matches) {
  // マウス環境: ホバーで追従する軽いポップアップ（区間の方向別内訳をすぐ見たい）
  const hoverPopup = new maplibregl.Popup({
    closeButton: false,
    closeOnClick: false,
    offset: 10,
    maxWidth: '280px',
  })
  map.on('mousemove', (e) => {
    const f = hitFeature(e.point)
    map.getCanvas().style.cursor = f ? 'pointer' : ''
    if (!f) {
      hoverPopup.remove()
      return
    }
    hoverPopup.setLngLat(e.lngLat).setHTML(htmlFor(f)).addTo(map)
  })
  map.on('mouseout', () => hoverPopup.remove())
} else {
  // タッチ環境: タップで閉じるボタン付きのポップアップ
  let popup: maplibregl.Popup | null = null
  map.on('click', (e) => {
    const f = hitFeature(e.point)
    if (popup) {
      popup.remove()
      popup = null
    }
    if (!f) return
    popup = new maplibregl.Popup({ closeButton: true, maxWidth: '280px' })
      .setLngLat(e.lngLat)
      .setHTML(htmlFor(f))
      .addTo(map)
  })
}

// ---- 初期化 ----
const buildEl = document.getElementById('build-ver')
if (buildEl) buildEl.textContent = `build: ${__BUILD_TIME__}`
renderThemeBtn()
buildToggles()
// スマホでは初期状態でパネルを畳んで地図を広く見せる
if (isMobile) panel.classList.add('collapsed')
renderCollapseBtn()
map.on('load', applyLayerState)

// WebGL コンテキスト消失からの復帰。iOS Safari 等ではメモリ逼迫時に GL コンテキストが
// 失われ、データ層がまるごと消えて戻らないことがある。復帰時に状態を貼り直す。
const canvas = map.getCanvas()
canvas.addEventListener('webglcontextlost', (e) => e.preventDefault(), false)
canvas.addEventListener(
  'webglcontextrestored',
  () => {
    if (map.isStyleLoaded()) applyLayerState()
    else map.once('idle', applyLayerState)
  },
  false,
)

// スクリーンショット用: タイル読み込み完了を外から待てるようにする
interface DebugWindow {
  __map: maplibregl.Map
  __idle: boolean
}
;(window as unknown as DebugWindow).__map = map
;(window as unknown as DebugWindow).__idle = false
map.on('idle', () => {
  ;(window as unknown as DebugWindow).__idle = true
})
