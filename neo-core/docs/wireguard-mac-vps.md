# WireGuard entre o Mac, o iPhone e a VPS

Desde 20/11/2026, o Mac fala com o neo-core da VPS por WireGuard. Isso substitui o Core Tunnel (túnel SSH com 11 portas encaminhadas), que caía quando a rede mudava ou o Mac dormia.

## Topologia

| Ponta | IP no túnel | Onde está a configuração |
|---|---|---|
| VPS Contabo `178.18.241.170` | `10.66.66.1` (interface `wg0`, UDP 51820) | `/etc/wireguard/wg0.conf` |
| iPhone | `10.66.66.2` (peer `celular`) | app WireGuard; cópia em `~/Downloads/wg-celular.conf` e QR em `wg-celular.png` |
| Mac | `10.66.66.3` (peer `mac`) | WireGuard.app, túnel `neo-vps`; a chave privada fica só no Keychain |

É um split tunnel: só `10.66.66.0/24` passa pelo túnel, e o resto da internet sai direto.

## O caminho de uma chamada do desktop

```
desktop / painel / extensão
  → 127.0.0.1:8000           socat (LaunchAgent no Mac)
  → utun                     WireGuard.app
  → 10.66.66.1:8000          o Docker publica no IP do wg0
  → container neo-core
```

O `socat` existe porque os clientes têm `127.0.0.1:8000` fixo em uns 20 lugares (painel, desktop e as duas extensões). Além disso, a política de conexões da extensão do navegador só aceita `127.0.0.1` e `localhost`, o callback do Spotify é `127.0.0.1`, e no Tauri a `NEO_CORE_URL` quer dizer duas coisas (endereço gRPC em `commands/mod.rs:93` e base HTTP no resto). Manter `127.0.0.1` evitou mudar código.

| Porta no Mac | Destino na VPS | Quem usa |
|---|---|---|
| 8000 | neo-core HTTP/WS | desktop, painel, extensões |
| 50051 | neo-core gRPC | desktop |
| 5434 | neo-postgres | `psql`, `db/seeds.py`, scripts |

**Zion (desde 20/11):** o Claude Code e os hooks do Mac falam direto com
`http://10.66.66.1:7077` (container `zion`), sem socat e com token Bearer
(`~/.config/zion/token` no Mac e na VPS). A porta só é publicada no IP do `wg0`, e um drop-in
do Docker (`/etc/systemd/system/docker.service.d/zion-7077.conf`) barra no `DOCKER-USER` o
que não entra pela `wg0`. Ver `zion-integracao-plan.md`.

Fora do túnel:
- **pontuax-redis** (6379): é de outro projeto e usa o ZeroTier.
- **neo-voice**: o desktop e o painel usam o da LAN (`192.168.4.210:5002`), não o da VPS.
- **Home Assistant e Portainer**: escutam em todos os IPs da VPS. Pelo túnel, abra direto `http://10.66.66.1:8123` e `:9000`. No IP público, o firewall da Contabo barra os dois.

O neo-core usa o IP do cliente só em log (`api/auth/principal.py`, `api/routes/auth.py`). A origem passou a ser `10.66.66.3` em vez do gateway do Docker, e nada depende disso.

## As peças

**VPS**
- `/etc/wireguard/wg0.conf` (backup em `wg0.conf.bak-20261120`). Além dos dois peers, tem um `PostUp` que descarta, na tabela `raw`, qualquer pacote que chegue pela `eth0` com destino `10.66.66.0/24`. O DNAT do Docker para `10.66.66.1` aceitaria um pacote forjado que viesse por fora; com essa regra, só o que sai do túnel alcança o postgres.
- `/etc/systemd/system/docker.service.d/after-wg.conf`: o Docker só sobe depois do `wg-quick@wg0`. Sem isso, a publicação em `10.66.66.1` falha no boot ("cannot assign requested address") e o neo-core não volta depois de um reboot.
- `docker-compose.override.yml` na raiz do monorepo. Fica fora do git (a lista de permitidos do `.gitignore` já o ignora), e o Mutagen o leva para a VPS. Ele **soma** `10.66.66.1` às portas `127.0.0.1` do compose base: neo-core 8000 e 50051, neo-postgres 5434. O `127.0.0.1` continua lá, então um túnel SSH ainda funciona como plano B.
- **Firewall da Contabo** (no painel, fora da VPS): tem uma regra de entrada liberando UDP 51820. Ver a seção abaixo.

**Mac**
- WireGuard.app, túnel `neo-vps`, com `PersistentKeepalive = 25`.
- `~/Library/LaunchAgents/dev.neo.wg-fwd.{8000,50051,5434}.plist`: um `socat` por porta (instalado pelo Homebrew), com `KeepAlive`, escutando só em `127.0.0.1`, com `connect-timeout=5`. Log em `~/Library/Logs/neo-wg-fwd-<porta>.log`.

## A pegadinha: o firewall da Contabo

Existe um firewall na frente da VPS, fora dela. Não é o `ufw`. Ele guarda estado: o tráfego que a VPS iniciou volta (é por isso que o ZeroTier funcionava), mas tráfego novo vindo de fora só passa nas portas liberadas no painel.

Antes de liberar o UDP 51820, nenhum handshake acontecia, nem o do iPhone. Medindo com `tcpdump` na `eth0`, nenhum UDP vindo do Mac chegava, em nenhuma porta. O UDP traceroute atravessava o provedor e morria depois da Arelion (salto 12). SYN TCP para 8123 e 9000 também não chega, mas para 5435 chega.

Para diagnosticar, rode `tcpdump -ni eth0 'udp port 51820'` na VPS enquanto o Mac pinga `10.66.66.1`. Espere aparecer o `listening on` antes de mandar o tráfego: o SSH até a VPS leva uns 4 segundos para conectar, e sem essa espera o teste dá falso negativo.

## Diagnóstico rápido

1. `scutil --nc list | grep neo-vps` deve mostrar `Connected`.
2. `ping 10.66.66.1` deve responder em cerca de 170 ms (Brasil–Europa).
3. Na VPS, como root: `wg show wg0 latest-handshakes`. Se der `0`, nunca houve handshake: veja o firewall da Contabo.
4. `lsof -nP -iTCP:8000 -sTCP:LISTEN` deve mostrar o `socat1`.
5. `curl -s -o /dev/null -w '%{http_code}' http://10.66.66.1:8000/health` testa o túnel sem passar pelo socat.
6. Na VPS: `ss -tln | grep 10.66.66.1`. Se estiver vazio, o Docker subiu antes do `wg0`; rode `docker compose up -d --no-deps neo-core neo-postgres`.

Com o túnel fora do ar, o desktop não vê mais "Connection refused": o socat aceita a conexão local e a fecha em até 5 segundos. O app reconecta sozinho quando o túnel volta.

## Rollback

Desligue o `neo-vps` no WireGuard.app, rode `launchctl bootout gui/$(id -u)/dev.neo.wg-fwd.8000` (e o mesmo para 50051 e 5434) e religue o Core Tunnel. A VPS continua publicando em `127.0.0.1`.

## Adicionar um aparelho

1. Gere o par de chaves no próprio aparelho (no app WireGuard, "Add empty tunnel").
2. Acrescente um `[Peer]` no `wg0.conf` com `AllowedIPs = 10.66.66.N/32` e rode `wg syncconf wg0 <(wg-quick strip wg0)`. Isso não derruba os outros peers.
3. No aparelho, use `Endpoint = 178.18.241.170:51820`, `AllowedIPs = 10.66.66.0/24` e `PersistentKeepalive = 25`.

## Pendências

- Ligar o **On-Demand** (Wi-Fi + Ethernet) no `neo-vps` do Mac, pela interface do WireGuard.app.
- iPhone: importar o QR e confirmar o handshake do `.2`.
- `ufw`: apagar as regras `8000`, `50051`, `50052` e `5002 ALLOW Anywhere`. Não servem para nada (essas portas estão em `127.0.0.1`, e o Docker ignora o `ufw`).
- Core Tunnel: apagar o perfil. As portas 3000, 3333, 9119 e 20128 não tinham nada escutando na VPS.
- Achado paralelo, fora do neo: `mr-abagnale-db` (Postgres, 5435) e `session-worker-proxy` (4100) respondem no IP público.
