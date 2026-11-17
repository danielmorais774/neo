import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import http from 'node:http'

/*
 * Proxy da Sirene (ESP32) — /sirene/<ip>/api/... → http://<ip>/api/...
 *
 * O firmware NÃO manda Access-Control-Allow-Origin e responde 404 no preflight
 * OPTIONS (medido em 2026-08-03 contra 192.168.4.85). Sem isso o browser
 * bloqueia tanto o GET quanto o POST, mesmo com o device respondendo 200 no
 * curl. E o neo-core não serve de ponte: ele roda na VPS e não enxerga a LAN.
 *
 * Então a ponte é o próprio dev server: mesma origem, CORS deixa de existir.
 * O IP vai na URL (e não em env) porque é DHCP e a tela deixa trocar em runtime.
 *
 * Some no build de produção — ali a tela chama o ESP32 direto, e aí o firmware
 * precisa mesmo mandar os cabeçalhos de CORS.
 */
const LAN_ONLY = /^(192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|[\w-]+\.local)(:\d+)?$/

function sireneProxy(): Plugin {
  return {
    name: 'sirene-proxy',
    configureServer(server) {
      server.middlewares.use('/sirene', (req, res, next) => {
        // O ESP32 responde com `Connection: close`, e destroy() no timeout
        // também emite 'error' — os dois chegam DEPOIS da resposta já ter ido
        // embora. Sem esta trava, o segundo fail() estoura ERR_HTTP_HEADERS_SENT
        // e derruba o dev server inteiro (não só a requisição).
        let answered = false
        const fail = (code: number, error: string) => {
          if (answered || res.headersSent) return
          answered = true
          res.statusCode = code
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify({ error }))
        }

        // connect já removeu o prefixo: sobra "/<ip>/api/status".
        //
        // Sem a forma de chamada de proxy, isto NÃO é erro: a tela do painel
        // também mora em /sirene (BrowserRouter), e recarregar a página faz o
        // browser pedir o documento em `GET /sirene` — que chega aqui com
        // req.url === "/". Respondendo 400 a gente devolvia JSON no lugar do
        // index.html e o painel inteiro morria em qualquer F5 na tela. Passando
        // adiante, o connect restaura a URL e o fallback SPA do vite atende.
        const match = /^\/([^/]+)(\/.*)$/.exec(req.url ?? '')
        if (!match) return next()

        const [, authority, path] = match
        // Sem isto o dev server vira proxy aberto pra qualquer host.
        if (!LAN_ONLY.test(authority)) return fail(403, `host fora da LAN: ${authority}`)

        const [host, port] = authority.split(':')

        // O corpo é bufferizado (e não repassado com pipe) pra poder ir com um
        // Content-Length explícito: sem ele o Node manda Transfer-Encoding:
        // chunked, e o servidor HTTP do ESP32 não lê corpo chunked — responde
        // "body ausente" com 400. Os payloads aqui são JSON de config, minúsculos.
        const chunks: Buffer[] = []
        req.on('data', (c: Buffer) => chunks.push(c))
        req.on('end', () => {
          const body = Buffer.concat(chunks)
          const upstream = http.request(
            {
              host,
              port: port ? Number(port) : 80,
              path,
              method: req.method,
              // O Host original é o dev server; repassar confundiria o ESP32.
              headers: body.length
                ? {
                    'content-type': req.headers['content-type'] ?? 'application/json',
                    'content-length': body.length,
                  }
                : {},
              timeout: 5000,
            },
            (up) => {
              answered = true
              res.writeHead(up.statusCode ?? 502, {
                'Content-Type': up.headers['content-type'] ?? 'application/json',
              })
              up.pipe(res)
            },
          )

          upstream.on('timeout', () => { fail(504, 'ESP32 não respondeu em 5s'); upstream.destroy() })
          upstream.on('error', (e) => fail(502, `ESP32 inacessível: ${e.message}`))
          upstream.end(body.length ? body : undefined)
        })
        req.on('error', (e) => fail(400, `falha lendo o corpo: ${e.message}`))
      })
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), sireneProxy()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // O painel fala DIRETO com o neo-voice (o neo-core na VPS não o alcança).
      //
      // Tem de ser o MESMO neo-voice que o desktop usa — o do neo-host na LAN.
      // Antes isto apontava p/ 127.0.0.1:5002, que hoje é o Core Tunnel pro
      // neo-voice velho da VPS: o painel listava vozes e ligava/desligava o
      // fallback numa instância que NÃO é a que fala no Modo Live (e o toggle do
      // hedge nem existe lá — dava "neo-voice indisponível").
      '/neo-voice': {
        target: process.env.NEO_VOICE_URL || 'http://192.168.4.210:5002',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/neo-voice/, ''),
      },
    },
  },
})
