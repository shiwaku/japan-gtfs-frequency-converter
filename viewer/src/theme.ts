export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'bus-frequency-theme'

function systemPref(): Theme {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function initialTheme(): Theme {
  // 旧ビューアの ?theme=dark / ?theme=light を引き続き効かせる（共有リンク用）
  const q = new URLSearchParams(location.search).get('theme')
  if (q === 'light' || q === 'dark') return q
  const saved = localStorage.getItem(STORAGE_KEY)
  return saved === 'light' || saved === 'dark' ? saved : systemPref()
}

/** <html data-theme="…"> を更新して現在テーマを保存する。 */
export function applyThemeAttr(theme: Theme): void {
  document.documentElement.dataset.theme = theme
  try {
    localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // プライベートブラウジング等で localStorage が使えない場合は保存しない
  }
}
