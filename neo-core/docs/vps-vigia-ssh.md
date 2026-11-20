# Vigia da VPS e SSH só com chave

Desde 20/11/2026, a VPS Contabo avisa no Discord quem entra nela, por SSH ou pelo WireGuard, e manda um resumo todo dia às 23:55 (horário de Fortaleza). No mesmo dia, o login por senha no SSH foi desligado. Para a VPN em si, ver [wireguard-mac-vps.md](wireguard-mac-vps.md).

## SSH só com chave

Antes, o SSH aceitava senha, inclusive como root, e recebia cerca de 67 mil tentativas por dia. Desde que o journal começa (20/09), só um login por senha deu certo: `daniel`, em 14/11, vindo do IP de casa.

- A regra fica em `/etc/ssh/sshd_config.d/01-somente-chave.conf`: `PasswordAuthentication no`, `KbdInteractiveAuthentication no` e `PermitRootLogin prohibit-password`. O nome começa com `01` de propósito: no sshd, a **primeira** definição vence, e o `50-cloud-init.conf` traz `PasswordAuthentication yes`.
- A única chave autorizada, para `root` e `daniel`, é `daniel@neo-docker` (`~/.ssh/id_ed25519_docker_neo` no Mac). O Mutagen e o docker context usam essa mesma chave.
- Sem a chave, o socorro é o **console VNC do painel da Contabo**. Ele entra pelo hipervisor, não depende do sshd e aceita a senha do root.
- Para conferir: `sshd -T | grep -E '^(passwordauthentication|permitrootlogin)'`. Um teste de senha vindo de fora deve receber `Permission denied (publickey)`.

## Vigia

O vigia é um script em Python que usa só a biblioteca padrão. Roda como root no serviço `vigia.service`.

| Onde | O quê |
|---|---|
| `/opt/vigia/vigia.py` | O código que roda (só o root escreve) |
| `neo-core/scripts/vps/vigia.py` e `vigia.service` | Cópia versionada. A VPS não roda daqui: o Mutagen escreve nesta pasta, e um root executando código de lá ficaria exposto |
| `/etc/vigia/discord-webhook` | URL do webhook (0600). Nunca vai para o log, para o repo ou para a memória |
| `/var/lib/vigia/estado.json` | Redes já vistas, contadores do período, posição no journal |
| `/var/lib/vigia/ip2asn-combined.tsv.gz` | Base de operadoras ([iptoasn.com](https://iptoasn.com)), baixada de novo a cada 7 dias. A consulta é local, e o IP não sai da VPS |

**Quando avisa**

| Evento | Aviso |
|---|---|
| 🚨 SSH aceito por senha | Sempre. A senha está desligada, então isso quer dizer que alguém a religou |
| 🚨 SSH com chave fora da lista do script | Sempre. A lista fica no código (`CHAVES_SSH`), não no `authorized_keys`, porque quem invade costuma plantar uma chave lá |
| 🚨 Peer de WireGuard sem `# Peer: nome` no `wg0.conf` | Uma vez por chave |
| 🔑 SSH com chave conhecida vindo de uma operadora nova | Uma vez por chave e operadora |
| 🔐 Mac ou iPhone conectando no WireGuard por uma operadora nova | Uma vez por aparelho e operadora |
| 🟢 vigia ligado | A cada início do serviço. Avisa também se a VPS acabou de ligar |
| 📋 Resumo | 23:55, horário de Fortaleza |

"Operadora" é o ASN do IP: a internet de casa, a operadora do celular. É por isso que o iPhone trocando de IP no 4G não gera aviso, e que o Mutagen e o Claude Code (dezenas de logins por dia, da mesma rede) ficam em silêncio. Se a base não carregar, o vigia usa a faixa `/16` do IP no lugar.

**O resumo** traz os logins SSH aceitos (agrupados por usuário, chave e operadora), as conexões barradas (cada conexão que fecha antes da autenticação conta uma vez) e os IPs que mais insistiram. Para cada aparelho do WireGuard, mostra se está conectado, por quais operadoras passou e quanto tráfego houve. Fecha com o número de alertas do período. O resumo também serve de sinal de vida: se ele não chegar num dia, o vigia caiu, ou alguém com root o desligou.

**Leitura do log:** o vigia segue o journal do `sshd` e guarda o carimbo (em µs) do último evento que processou. Ao religar, relê a partir dali e pula o que já viu. Assim, um login feito enquanto o vigia estava fora (logo depois de um boot, por exemplo) também gera aviso, sem repetir os antigos.

**Limitação:** quem tiver root na VPS consegue desligar o vigia. O aviso do próprio login sai em cerca de 1 segundo, antes disso, e a falta do resumo diário denuncia o resto. Se o Discord estiver fora do ar, o vigia tenta 5 vezes e depois só registra no log (`[discord] DESISTI`).

## Operação

- Mandar o resumo agora, sem zerar o período: `systemctl reload vigia`.
- Testar o webhook: `python3 /opt/vigia/vigia.py teste`.
- Log: `journalctl -u vigia -f`. As linhas `[alerta]` são o que foi para o Discord.
- Trocar o webhook: escreva a nova URL em `/etc/vigia/discord-webhook` e pronto; ela é lida a cada envio.
- Autorizar uma chave SSH nova: some a impressão digital (`ssh-keygen -lf chave.pub`) em `CHAVES_SSH`, no repo e em `/opt/vigia/vigia.py`, e rode `systemctl restart vigia`.
- Mudar o código: edite `neo-core/scripts/vps/vigia.py`, copie para `/opt/vigia/` como root e rode `systemctl restart vigia`.
