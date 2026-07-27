import { defineConfig, type Plugin } from 'vite'
import { createReadStream, existsSync, statSync } from 'node:fs'
import { join, normalize, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const rootDir = fileURLToPath(new URL('.', import.meta.url))
// 集計パイプラインの成果物（PMTiles）はリポジトリ直下 build/ にある。
const PMTILES_DIR = resolve(rootDir, '..', 'build')

/**
 * 開発サーバーで build/*.pmtiles を Range(206) 対応で配信するミドルウェア。
 * pmtiles.js は HTTP Byte Serving を要求するため、これがないとタイルを読めない。
 * 本番（GitHub Pages）では PMTiles を dist/pmtiles/ に同梱し、静的ホストが Range を処理する。
 */
function pmtilesDevServer(): Plugin {
  return {
    name: 'pmtiles-dev-server',
    configureServer(server) {
      server.middlewares.use('/pmtiles', (req, res, next) => {
        try {
          const urlPath = decodeURIComponent((req.url ?? '').split('?')[0])
          const rel = normalize(urlPath).replace(/^([/\\]|\.\.[/\\])+/, '')
          const file = join(PMTILES_DIR, rel)
          if (!file.startsWith(PMTILES_DIR) || !existsSync(file)) {
            res.statusCode = 404
            res.end('Not found')
            return
          }
          const size = statSync(file).size
          res.setHeader('Accept-Ranges', 'bytes')
          res.setHeader('Access-Control-Allow-Origin', '*')
          res.setHeader('Content-Type', 'application/octet-stream')

          const range = req.headers.range
          const m = range ? /bytes=(\d*)-(\d*)/.exec(range) : null
          if (m) {
            const start = m[1] ? parseInt(m[1], 10) : 0
            let end = m[2] ? parseInt(m[2], 10) : size - 1
            end = Math.min(end, size - 1)
            if (start > end || start >= size) {
              res.statusCode = 416
              res.setHeader('Content-Range', `bytes */${size}`)
              res.end()
              return
            }
            res.statusCode = 206
            res.setHeader('Content-Range', `bytes ${start}-${end}/${size}`)
            res.setHeader('Content-Length', String(end - start + 1))
            createReadStream(file, { start, end }).pipe(res)
            return
          }

          res.statusCode = 200
          res.setHeader('Content-Length', String(size))
          createReadStream(file).pipe(res)
        } catch (err) {
          next(err)
        }
      })
    },
  }
}

export default defineConfig({
  // 相対パスで出力する。ローカル (/) でも Pages のサブパス (/<repo>/) でも同じ成果物で通る
  base: './',
  plugins: [pmtilesDevServer()],
  server: { port: 8000 },
  define: {
    __BUILD_TIME__: JSON.stringify(new Date().toISOString().replace('T', ' ').slice(0, 16) + ' UTC'),
  },
})
