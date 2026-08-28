import type {
  CircleLayerSpecification,
  ExpressionSpecification,
  LineLayerSpecification,
  SymbolLayerSpecification,
} from 'maplibre-gl'
import type { Basemap } from './basemap'
import type { Theme } from './theme'

// ---- 運行本数の配色 ----
// 便数の分布は 1-5:21% / 6-11:27% / 12-23:24% / 24-47:14% / 48-95:6.8% / 96+:6.9%。
// 等間隔ではなくおおよそ倍々で刻む。太さも同じ階級で変え、色と二重に符号化する。
//
// 寒色→暖色（少ない=青 / 多い=赤）。単一色相の濃淡だと、便数の多い区間が集まる都市部で
// 上位3階級がひとかたまりの濃紺に見えてしまい、幹線がどこかを読めなかった。色相を振ると
// 24–47 と 48–95 の境目が線の太さに頼らず分かる。
//
// 明度は OKLCH で単調に下降させてあり（明 0.76 → 暗 0.45）、色相だけに頼らないので
// 色覚特性やモノクロ印刷でも順序は保たれる。最も薄い階級も地色に対して 2.09:1 あり、
// 旧配色（1.50:1）で全体の21%を占める「1–5便」がほぼ見えなかった問題も解消している。
export const BREAKS = [6, 12, 24, 48, 96]
export const LABELS = ['1–5', '6–11', '12–23', '24–47', '48–95', '96+']
export const RAMP: Record<'light' | 'dark', string[]> = {
  // 明るい地図: 低頻度ほど淡い青、幹線ほど濃い赤
  light: ['#85b4f0', '#888dec', '#a95ec8', '#bc3181', '#ba022f', '#9e1901'],
  // 暗い地図・写真: 明度の向きを反転（地色に近い＝低頻度）。色相の並びは同じ。
  // OKLCH で L 0.50→0.78 を等間隔（ΔL 0.055〜0.057）に取り、各段の彩度は sRGB の上限。
  // 前の配色は両端で無理をしていた。最下位が #234c88 で地色 #0d0d0d に対し 2.28:1、
  // 背景が紺色になる z4–7（#121e2f）では 1.96:1 まで落ち、最細 1.0px の線が沈んでいた。
  // 最上位は #fec583（C=0.105）で、明るい地図側の同階級（C=0.170）より4割彩度が低く、
  // 幹線が一番ぼやけた色になっていた。今は 3.27:1 / C=0.169。
  //
  // 上端が赤ではなく琥珀（色相 70°）なのは sRGB の都合。L=0.78 で確保できる彩度は
  // 色相 45°（朱）だと 0.137 までで、そこで止めた案は彩度も protanopia 下の隣接 ΔE
  // （3.7）も落ちた。70° まで回すと 0.169 を保てる。
  dark: ['#0068a7', '#685ed7', '#b159c6', '#e65c97', '#ff765a', '#fba100'],
}
// 太さも色と同じ6階級で振り、色に頼らなくても本数の多寡が読めるようにする。
// 幅は等比（約1.5倍ずつ）で 1:7.6。等差にすると上位側の比が詰まり、24–47 と 48–95 が
// 同じ太さに見えてしまう。下限は 1.0px で、最も細い階級も線として残る。
export const WIDTHS = [1.0, 1.5, 2.2, 3.4, 5.1, 7.6]

export interface LayerDef {
  key: string
  name: string
  desc: string
  on: boolean
}

/** 配列の後ろほど地図で前面。区間 → 停留所 → 停留所名 の順に重ねる。 */
export const LAYERS: LayerDef[] = [
  {
    key: 'routes',
    name: '区間の運行本数',
    on: true,
    desc: '停留所どうしの区間を、平日1日の便数（両方向の合計）で6段階に塗り分けます。色が濃く太いほど本数が多い区間です。',
  },
  {
    key: 'stops',
    name: '停留所',
    on: true,
    desc: 'GTFS の停留所。同名かつ近接するものは1つにまとめてあります。区間の線より前面に、白抜きの丸で描きます。z11 以上で表示。',
  },
  {
    key: 'stop-labels',
    name: '停留所名',
    on: true,
    desc: 'z14 で停留所名を表示します。重なる場合は停車回数の多い停留所を優先します。',
  },
]

export const defOf = (key: string): LayerDef | undefined => LAYERS.find((d) => d.key === key)

export interface Palette {
  /** 便数6階級の色 */
  colors: string[]
  /** 文字・停留所の輪郭に使う色 */
  ink: string
  /** 停留所の塗り（地色側） */
  fill: string
  /** ラベルのハロー */
  halo: string
}

/** 写真の上では暗い地図と同じ「明るい線」の向きにする。 */
export const isDarkCanvas = (theme: Theme, base: Basemap): boolean => theme === 'dark' || base === 'photo'

export function paletteFor(theme: Theme, base: Basemap): Palette {
  const darkCanvas = isDarkCanvas(theme, base)
  return {
    colors: darkCanvas ? RAMP.dark : RAMP.light,
    ink: darkCanvas ? '#f2f4f7' : '#14161a',
    fill: darkCanvas ? '#14161a' : '#ffffff',
    halo: base === 'photo' ? 'rgba(0,0,0,.8)' : darkCanvas ? 'rgba(12,14,18,.85)' : 'rgba(255,255,255,.9)',
  }
}

/** step 式: [値0, 閾値0, 値1, 閾値1, …] を frequency から引く */
const stepExpr = (vals: (string | number)[]): ExpressionSpecification => {
  const e: unknown[] = ['step', ['get', 'frequency'], vals[0]]
  BREAKS.forEach((b, i) => e.push(b, vals[i + 1]))
  // 可変長の step 式は型で表現できないため、組み立てたうえで式型として扱う
  return e as unknown as ExpressionSpecification
}

/**
 * どのズームでも階級間の太さの比を保つ。以前は低ズームで 1.0–1.2px の下限に丸めていて、
 * 下位3階級が同じ太さに潰れ、太さが本数を表していなかった。丸めるかわりに全体を
 * 縮小率で調整する。z9 未満は便数の少ない区間がそもそもタイルに入らない
 * （aggregate.py が便数ごとに minzoom を決めている）ので、細くしても線は消えない。
 */
const lineWidthExpr = (): ExpressionSpecification =>
  [
    'interpolate',
    ['linear'],
    ['zoom'],
    4,
    ['max', ['*', stepExpr(WIDTHS), 0.45], 0.9],
    6,
    ['max', ['*', stepExpr(WIDTHS), 0.55], 0.9],
    9,
    ['*', stepExpr(WIDTHS), 0.8],
    12,
    stepExpr(WIDTHS),
    14,
    ['*', stepExpr(WIDTHS), 1.25],
  ] as unknown as ExpressionSpecification

type DataLayer = LineLayerSpecification | CircleLayerSpecification | SymbolLayerSpecification

export function dataLayers(p: Palette): DataLayer[] {
  return [
    {
      id: 'routes',
      type: 'line',
      source: 'bus',
      'source-layer': 'routes',
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': stepExpr(p.colors),
        'line-width': lineWidthExpr(),
        // わずかに透かして、太い幹線の下の地図と交差する区間を読めるようにする
        'line-opacity': 0.9,
      },
    },
    {
      // 区間より前面。塗りつぶしの円だけだと幹線（最大 10px 弱）に飲まれて見えないので、
      // 地色で塗って輪郭を付けた「白抜きの丸」にし、線の上に乗っていることを分かるようにする。
      id: 'stops',
      type: 'circle',
      source: 'bus',
      'source-layer': 'stops',
      // タイル側の最小ズームが 11 なので、それ未満を指定しても何も出ない。
      // 中ズームで大きくすると数百個の丸が地図を埋めてノイズになるため、
      // z11–12 は控えめにして z14 で十分な大きさになるよう効かせる。
      minzoom: 11,
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 1.3, 12, 1.9, 13, 2.8, 14, 4.2],
        'circle-color': p.fill,
        'circle-stroke-color': p.ink,
        'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 11, 0.5, 13, 1, 14, 1.5],
      },
    },
    {
      // 停留所名。z14（最大ズーム）でのみ出す。重なりは MapLibre に間引かせ、
      // 停車回数の多い停留所が残るように sort-key を効かせる。
      id: 'stop-labels',
      type: 'symbol',
      source: 'bus',
      'source-layer': 'stops',
      minzoom: 14,
      layout: {
        'text-field': ['get', 'name'],
        'text-font': ['NotoSansJP-Regular'],
        'text-size': 11,
        'text-offset': [0, 0.9],
        'text-anchor': 'top',
        'text-max-width': 8,
        'text-padding': 3,
        'symbol-sort-key': ['-', 0, ['coalesce', ['get', 'count'], 0]],
      },
      paint: {
        'text-color': p.ink,
        'text-halo-color': p.halo,
        'text-halo-width': 1.4,
      },
    },
  ]
}

/** パネル内の凡例。区間は線そのものを見せたいので太さも本文と同じ比率で描く。 */
export function legendMarkup(def: LayerDef, p: Palette): string {
  if (def.key === 'routes') {
    return (
      LABELS.map(
        (l, i) =>
          `<span class="lg-row"><span class="lg-bar" style="height:${Math.max(WIDTHS[i] * 1.25, 1.5)}px;background:${p.colors[i]}"></span>${l}</span>`,
      ).join('') + '<span class="lg-note">便/日（両方向計）</span>'
    )
  }
  if (def.key === 'stops') {
    return `<span class="lg-row"><span class="lg-dot" style="background:${p.fill};border-color:${p.ink}"></span>停留所（z11以上）</span>`
  }
  return '<span class="lg-row">z14 で表示</span>'
}

// ---- ポップアップ ----
const esc = (v: unknown): string =>
  String(v ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] as string)

function routeHtml(p: Record<string, unknown>): string {
  return (
    `<div class="pp-title">${esc(p.stop_a)} ⇄ ${esc(p.stop_b)}</div>` +
    '<div class="pp-sub">区間の運行本数（平日・両方向計）</div>' +
    '<dl class="pp-dl">' +
    `<dt>便/日</dt><dd class="pp-strong">${esc(p.frequency)}</dd>` +
    `<dt>→ ${esc(p.stop_b)}</dt><dd>${esc(p.frequency_ab)}</dd>` +
    `<dt>← ${esc(p.stop_a)}</dt><dd>${esc(p.frequency_ba)}</dd>` +
    `<dt>事業者</dt><dd>${esc(p.operators)}</dd>` +
    `<dt>系統</dt><dd>${esc(p.routes)}</dd>` +
    '</dl>' +
    `<div class="pp-foot">${esc(p.agency)}</div>`
  )
}

function stopHtml(p: Record<string, unknown>): string {
  return (
    `<div class="pp-title">${esc(p.name)}</div>` +
    '<div class="pp-sub">停留所</div>' +
    '<dl class="pp-dl">' +
    `<dt>停車回数/日</dt><dd class="pp-strong">${esc(p.count)}</dd>` +
    `<dt>事業者</dt><dd>${esc(p.operators)}</dd>` +
    '</dl>' +
    `<div class="pp-foot">${esc(p.agency)}</div>`
  )
}

export function popupHtml(layerId: string, props: Record<string, unknown>): string {
  return layerId === 'stops' ? stopHtml(props) : routeHtml(props)
}
