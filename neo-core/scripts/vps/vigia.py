#!/usr/bin/env python3
"""Vigia da VPS: avisa no Discord quem entra (SSH e WireGuard) e manda um resumo por dia.

Instalado em /opt/vigia/vigia.py (root). Serviço: vigia.service.
Estado em /var/lib/vigia. Webhook em /etc/vigia/discord-webhook (0600, nunca no log).
Documentação: neo-core/docs/vps-vigia-ssh.md, no monorepo do Neo.

Sinais: USR1 manda o resumo agora (teste). TERM salva o estado e sai.
"""
import array
import bisect
import gzip
import ipaddress
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

FUSO = ZoneInfo("America/Fortaleza")
HORA_RESUMO = (23, 55)
DIR = "/var/lib/vigia"
ESTADO = f"{DIR}/estado.json"
ASN_DB = f"{DIR}/ip2asn-combined.tsv.gz"
ASN_URL = "https://iptoasn.com/data/ip2asn-combined.tsv.gz"
ASN_VALIDADE = 7 * 86400
WEBHOOK = "/etc/vigia/discord-webhook"
WG_IF = "wg0"
WG_CONF = "/etc/wireguard/wg0.conf"
WG_INTERVALO = 20   # s entre leituras do `wg show`
WG_ONLINE = 180     # s: handshake mais novo que isso = conectado
SALVAR_A_CADA = 60  # s
MAX_IPS_FALHA = 20000
USER_AGENT = "vigia-vps/1.0"

# Impressão digital -> rótulo. Chave fora desta lista gera alerta urgente, mesmo que esteja no
# authorized_keys: quem invade costuma plantar uma chave lá. Não ler a lista do authorized_keys.
CHAVES_SSH = {
    "SHA256:/uJYFbRBJ8rTbMUUfQHHfiFKVNL872xNFR3UaDk/PD8": "chave do Mac (daniel@neo-docker)",
}
APELIDOS_WG = {"celular": "iPhone", "mac": "Mac"}
METODOS = {"password": "SENHA", "keyboard-interactive": "SENHA (teclado)"}

RE_ACEITO = re.compile(r"^Accepted (\S+) for (\S+) from (\S+) port \d+ ssh2(?:: \S+ (\S+))?")
RE_FALHA = re.compile(r"^(?:Connection closed by|Disconnected from) (?:.* )?(\S+) port \d+ \[preauth\]$")

INICIO = time.time()


def log(msg):
    print(msg, flush=True)


def fmt_hora(ts):
    return datetime.fromtimestamp(ts, FUSO).strftime("%d/%m %H:%M")


def fmt_num(n):
    return f"{n:,}".replace(",", ".")


def fmt_bytes(n):
    for unidade in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unidade}" if unidade == "B" else f"{n:.1f} {unidade}".replace(".", ",")
        n /= 1024
    return f"{n:.1f} TB".replace(".", ",")


# ---------------------------------------------------------------- Discord
class Discord(threading.Thread):
    """Fila + uma thread: quem detecta não fica esperando a rede."""

    def __init__(self):
        super().__init__(daemon=True)
        self.fila = queue.Queue()

    def enviar(self, texto):
        self.fila.put(texto)

    def run(self):
        while True:
            self.postar(self.fila.get())

    def postar(self, texto):
        try:
            with open(WEBHOOK) as f:
                url = f.read().strip()
        except OSError as e:
            log(f"[discord] sem webhook: {e}")
            return False
        corpo = json.dumps({
            "content": texto[:1990],
            "username": "vigia-vps",
            "allowed_mentions": {"parse": []},
        }).encode()
        for tentativa in range(5):
            req = urllib.request.Request(url, data=corpo, method="POST", headers={
                "Content-Type": "application/json", "User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    log(f"[discord] enviado ({r.status})")
                    return True
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(float(e.headers.get("Retry-After") or 2))
                    continue
                log(f"[discord] HTTP {e.code}: {e.read()[:200]!r}")
                if 400 <= e.code < 500:
                    break
            except Exception as e:  # rede fora, DNS, timeout
                log(f"[discord] falha: {type(e).__name__}: {e}")
            time.sleep(min(60, 3 * 2 ** tentativa))
        log("[discord] DESISTI: " + texto.replace("\n", " | ")[:600])
        return False


# ---------------------------------------------------------------- operadora do IP (offline)
def nome_curto(nome):
    """'AS264472 - Fulano - Me - Bcnet Play' -> 'Bcnet Play'; 'GOOGLE - Google LLC' -> 'Google LLC'."""
    nome = re.sub(r"^AS\d+\s*-\s*", "", nome.strip())
    return nome.rsplit(" - ", 1)[-1].strip() or nome


class Redes:
    """Descobre a operadora (ASN) de um IP numa base baixada para a VPS: o IP não sai daqui."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tab = None

    def carregar(self):
        try:
            v4s, v4e, v4a = array.array("I"), array.array("I"), array.array("I")
            v6s, v6e, v6a = [], [], array.array("I")
            info = {}
            with gzip.open(ASN_DB, "rt", encoding="utf-8", errors="replace") as f:
                for linha in f:
                    p = linha.rstrip("\n").split("\t")
                    if len(p) < 5 or p[2] == "0":
                        continue
                    ini, fim, asn = ipaddress.ip_address(p[0]), ipaddress.ip_address(p[1]), int(p[2])
                    info.setdefault(asn, (p[3], nome_curto(p[4])))
                    if ini.version == 4:
                        v4s.append(int(ini)); v4e.append(int(fim)); v4a.append(asn)
                    else:
                        v6s.append(int(ini)); v6e.append(int(fim)); v6a.append(asn)
            for s in (v4s, v6s):
                if any(s[i] > s[i + 1] for i in range(len(s) - 1)):
                    raise ValueError("base fora de ordem")
            with self.lock:
                self.tab = (v4s, v4e, v4a, v6s, v6e, v6a, info)
            log(f"[asn] base carregada: {fmt_num(len(v4s))} faixas v4, {fmt_num(len(v6s))} v6")
        except Exception as e:
            log(f"[asn] sem base ({type(e).__name__}: {e}); uso a faixa do IP")

    def atualizar(self):
        try:
            idade = time.time() - os.path.getmtime(ASN_DB)
        except OSError:
            idade = None
        if idade is not None and idade < ASN_VALIDADE:
            if self.tab is None:
                self.carregar()
            return
        tmp = ASN_DB + ".tmp"
        try:
            req = urllib.request.Request(ASN_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
                while bloco := r.read(1 << 16):
                    f.write(bloco)
            with gzip.open(tmp, "rt") as g:
                g.readline()
            os.replace(tmp, ASN_DB)
            log("[asn] base baixada")
        except Exception as e:
            log(f"[asn] download falhou: {type(e).__name__}: {e}")
        self.carregar()

    def rede(self, ip):
        """-> (id estável, descrição legível)."""
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return ip, ip
        if a.version == 6 and a.ipv4_mapped:
            a = a.ipv4_mapped
        if not a.is_global:
            return "privada", "rede privada"
        with self.lock:
            tab = self.tab
        if tab:
            s, e, asns = (tab[0], tab[1], tab[2]) if a.version == 4 else (tab[3], tab[4], tab[5])
            n = int(a)
            i = bisect.bisect_right(s, n) - 1
            if i >= 0 and n <= e[i]:
                pais, nome = tab[6].get(asns[i], ("", "?"))
                return f"AS{asns[i]}", f"AS{asns[i]} {nome} ({pais})"
        faixa = ipaddress.ip_network(f"{a}/{16 if a.version == 4 else 32}", strict=False)
        return str(faixa), f"faixa {faixa}"


# ---------------------------------------------------------------- estado
class Estado:
    def __init__(self):
        self.lock = threading.RLock()
        try:
            with open(ESTADO) as f:
                self.d = json.load(f)
        except (OSError, ValueError):
            self.d = {}
        self.d.setdefault("redes", {})
        self.d.setdefault("ultimo_resumo", "")
        self.d.setdefault("wg_base", {})
        self.d.setdefault("dia", self.dia_novo())
        self.wg_atual = {}  # chave -> dict(nome, ip, hs, rx, tx, online); só em memória
        self.sujo = True

    @staticmethod
    def dia_novo():
        return {"inicio": time.time(), "ssh": {}, "falhas": 0, "falhas_ip": {}, "wg": {}, "alertas": 0}

    def salvar(self):
        with self.lock:
            tmp = ESTADO + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.d, f)
            os.replace(tmp, ESTADO)
            self.sujo = False

    def rede_nova(self, quem, rid, desc):
        """True se `quem` nunca apareceu na rede `rid`. Já registra."""
        with self.lock:
            vistas = self.d["redes"].setdefault(quem, {})
            agora = time.time()
            if rid in vistas:
                vistas[rid]["ultima"] = agora
                return False
            vistas[rid] = {"desc": desc, "primeira": agora, "ultima": agora}
            self.sujo = True
            return True


def alerta(est, discord, texto):
    with est.lock:
        est.d["dia"]["alertas"] += 1
        est.sujo = True
    log("[alerta] " + texto.replace("\n", " | "))
    discord.enviar(texto)


# ---------------------------------------------------------------- SSH
def tratar_ssh(msg, quando, est, redes, discord):
    m = RE_ACEITO.match(msg)
    if m:
        metodo, usuario, ip, digital = m.groups()
        rid, desc = redes.rede(ip)
        rotulo = CHAVES_SSH.get(digital) if metodo == "publickey" else None
        nome = rotulo or METODOS.get(metodo) or f"chave desconhecida {digital or metodo}"
        with est.lock:
            ssh = est.d["dia"]["ssh"]
            k = f"{usuario}|{nome}|{desc}"
            ssh[k] = ssh.get(k, 0) + 1
            est.sujo = True
        log(f"[ssh] aceito {metodo} {usuario} {ip} {desc}")
        hora = fmt_hora(quando)
        if metodo != "publickey":
            alerta(est, discord,
                   f"🚨 **SSH aceito por {METODOS.get(metodo, metodo)}** · usuário `{usuario}` · {ip} · {desc} · {hora}\n"
                   "O login por senha está desligado; se isto apareceu, alguém religou. Se não foi você, trate a VPS como invadida.")
        elif rotulo is None:
            alerta(est, discord,
                   f"🚨 **SSH com chave DESCONHECIDA** · usuário `{usuario}` · {ip} · {desc} · {hora}\n"
                   f"Chave `{digital}`. A única conhecida é a do Mac: confira os authorized_keys.")
        elif est.rede_nova(f"ssh:{digital}", rid, desc):
            alerta(est, discord, f"🔑 SSH de uma rede nova · `{usuario}` com {rotulo} · {ip} · {desc} · {hora}")
        return
    m = RE_FALHA.match(msg)
    if m:
        ip = m.group(1)
        with est.lock:
            dia = est.d["dia"]
            dia["falhas"] += 1
            fip = dia["falhas_ip"]
            if ip in fip or len(fip) < MAX_IPS_FALHA:
                fip[ip] = fip.get(ip, 0) + 1
            est.sujo = True


def seguir_ssh(est, redes, discord):
    """Segue o journal do sshd. Ao religar, relê desde o último evento visto: login feito
    enquanto o vigia estava fora (ex.: logo depois do boot) também avisa."""
    while True:
        with est.lock:
            ultimo_us = int(est.d.get("ssh_us") or time.time() * 1e6)
        cmd = ["journalctl", "-f", "-n", "all", f"--since=@{ultimo_us // 1_000_000}", "-o", "json",
               "SYSLOG_IDENTIFIER=sshd", "SYSLOG_IDENTIFIER=sshd-session"]
        try:
            # o filho ignora o USR1 (pedido de resumo): `systemctl kill` manda o sinal ao cgroup inteiro
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                 preexec_fn=lambda: signal.signal(signal.SIGUSR1, signal.SIG_IGN))
            log(f"[ssh] seguindo o journal desde {fmt_hora(ultimo_us / 1e6)}")
            for linha in p.stdout:
                try:
                    ev = json.loads(linha)
                except ValueError:
                    continue
                us = int(ev.get("__REALTIME_TIMESTAMP") or 0)
                if us and us <= ultimo_us:
                    continue  # já processado: o --since só tem precisão de segundo
                msg = ev.get("MESSAGE") or ""
                if isinstance(msg, list):
                    msg = bytes(msg).decode("utf-8", "replace")
                tratar_ssh(msg.strip(), us / 1e6 if us else time.time(), est, redes, discord)
                if us:
                    ultimo_us = us
                    with est.lock:
                        est.d["ssh_us"] = us
            p.wait()
            log(f"[ssh] journalctl saiu ({p.returncode})")
        except Exception as e:
            log(f"[ssh] erro: {type(e).__name__}: {e}")
        time.sleep(5)


# ---------------------------------------------------------------- WireGuard
def nomes_wg():
    """PublicKey -> nome, pelos comentários `# Peer: nome` do wg0.conf."""
    nomes, pendente = {}, None
    try:
        with open(WG_CONF) as f:
            for linha in f:
                l = linha.strip()
                m = re.match(r"#\s*Peer:\s*(.+)", l)
                if m:
                    pendente = re.sub(r"\s*\(.*\)\s*$", "", m.group(1)).strip()
                    continue
                m = re.match(r"PublicKey\s*=\s*(\S+)", l)
                if m and pendente:
                    nomes[m.group(1)] = APELIDOS_WG.get(pendente, pendente)
                    pendente = None
    except OSError as e:
        log(f"[wg] não li {WG_CONF}: {e}")
    return nomes


def vigiar_wg(est, redes, discord):
    antes = {}  # chave -> (ip, online)
    desconhecidos = set()
    while True:
        try:
            nomes = nomes_wg()
            r = subprocess.run(["wg", "show", WG_IF, "dump"], capture_output=True, text=True, timeout=10)
            agora = time.time()
            for linha in r.stdout.splitlines()[1:]:
                c = linha.split("\t")
                if len(c) < 8:
                    continue
                chave, endpoint, hs, rx, tx = c[0], c[2], int(c[4]), int(c[5]), int(c[6])
                nome = nomes.get(chave)
                if nome is None and chave not in desconhecidos:
                    desconhecidos.add(chave)
                    alerta(est, discord, f"🚨 **Peer de WireGuard sem nome no wg0.conf** · chave `{chave[:12]}…`\n"
                                         "Alguém pode ter cadastrado um aparelho na VPN.")
                nome = nome or f"peer {chave[:8]}…"
                ip = None if endpoint == "(none)" else endpoint.rsplit(":", 1)[0].strip("[]")
                online = bool(ip) and hs > 0 and agora - hs < WG_ONLINE
                with est.lock:
                    est.wg_atual[chave] = {"nome": nome, "ip": ip, "hs": hs, "rx": rx, "tx": tx, "online": online}
                ip_antes, online_antes = antes.get(chave, (None, False))
                if online:
                    rid, desc = redes.rede(ip)
                    conectou = not online_antes or ip != ip_antes
                    with est.lock:
                        # a rede entra no resumo mesmo se o aparelho já estava conectado antes do período
                        w = est.d["dia"]["wg"].setdefault(chave, {"nome": nome, "redes": {}, "conexoes": 0})
                        w["redes"].setdefault(desc, 0)
                        if conectou:
                            w["conexoes"] += 1
                            w["redes"][desc] += 1
                        est.sujo = True
                    if conectou:
                        log(f"[wg] {nome} conectou de {ip} ({desc})")
                        if est.rede_nova(f"wg:{chave}", rid, desc):
                            alerta(est, discord, f"🔐 WireGuard: **{nome}** conectou de uma rede nova · {ip} · {desc} · {fmt_hora(hs)}")
                antes[chave] = (ip, online)
        except Exception as e:
            log(f"[wg] erro: {type(e).__name__}: {e}")
        time.sleep(WG_INTERVALO)


# ---------------------------------------------------------------- resumo
def montar_resumo(est):
    with est.lock:
        d = json.loads(json.dumps(est.d["dia"]))
        base = dict(est.d["wg_base"])
        atual = json.loads(json.dumps(est.wg_atual))
    agora = datetime.now(FUSO).strftime("%d/%m %H:%M")
    l = [f"📋 **Resumo da VPS** · {fmt_hora(d['inicio'])} → {agora}"]
    total = sum(d["ssh"].values())
    l.append(f"**SSH:** {fmt_num(total)} login(s) aceito(s)")
    for k, n in sorted(d["ssh"].items(), key=lambda kv: -kv[1])[:8]:
        usuario, nome, desc = k.split("|", 2)
        l.append(f"• `{usuario}` · {nome} · {desc} · ×{fmt_num(n)}")
    top = sorted(d["falhas_ip"].items(), key=lambda kv: -kv[1])[:3]
    barradas = f"**Conexões barradas:** {fmt_num(d['falhas'])} de {fmt_num(len(d['falhas_ip']))} IPs"
    if top:
        barradas += " · mais insistentes: " + ", ".join(f"{ip} ×{fmt_num(n)}" for ip, n in top)
    l.append(barradas)
    l.append("**WireGuard:**")
    for chave, a in sorted(atual.items(), key=lambda kv: kv[1]["nome"]):
        b = base.get(chave, [0, 0])
        drx = a["rx"] - b[0] if a["rx"] >= b[0] else a["rx"]
        dtx = a["tx"] - b[1] if a["tx"] >= b[1] else a["tx"]
        if a["online"]:
            situacao = "conectado agora"
        elif a["hs"]:
            situacao = f"visto por último {fmt_hora(a['hs'])}"
        else:
            situacao = "sem conexão desde que a VPN subiu"
        redes_dia = ", ".join(d["wg"].get(chave, {}).get("redes", {})) or "não conectou no período"
        l.append(f"• {a['nome']}: {situacao} · {redes_dia} · baixou {fmt_bytes(dtx)}, enviou {fmt_bytes(drx)}")
    l.append(f"Alertas no período: {d['alertas']} · vigia ligado desde {fmt_hora(INICIO)}")
    return "\n".join(l)


def fechar_dia(est):
    with est.lock:
        est.d["dia"] = Estado.dia_novo()
        est.d["wg_base"] = {k: [a["rx"], a["tx"]] for k, a in est.wg_atual.items()}
        est.d["ultimo_resumo"] = datetime.now(FUSO).date().isoformat()
        est.sujo = True
    est.salvar()


# ---------------------------------------------------------------- main
def main():
    os.makedirs(DIR, exist_ok=True)
    discord = Discord()
    if sys.argv[1:] == ["teste"]:
        sys.exit(0 if discord.postar("🧪 Teste do vigia-vps: o webhook funciona.") else 1)

    est = Estado()
    redes = Redes()
    redes.atualizar()
    discord.start()

    resumo_ja = threading.Event()
    parar = threading.Event()
    signal.signal(signal.SIGUSR1, lambda *_: resumo_ja.set())
    signal.signal(signal.SIGTERM, lambda *_: parar.set())

    threading.Thread(target=seguir_ssh, args=(est, redes, discord), daemon=True).start()
    threading.Thread(target=vigiar_wg, args=(est, redes, discord), daemon=True).start()

    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    boot = f" · a VPS ligou às {fmt_hora(time.time() - uptime)}" if uptime < 900 else ""
    discord.enviar(f"🟢 vigia ligado{boot}")

    ultimo_save = ultimo_asn = time.time()
    while not parar.is_set():
        parar.wait(10)
        agora = datetime.now(FUSO)
        hoje = agora.date().isoformat()
        if resumo_ja.is_set() or ((agora.hour, agora.minute) >= HORA_RESUMO and est.d["ultimo_resumo"] != hoje):
            manual = resumo_ja.is_set()
            resumo_ja.clear()
            texto = montar_resumo(est)
            discord.enviar(texto if not manual else texto + "\n_(resumo pedido à mão; o período não foi zerado)_")
            if not manual:
                fechar_dia(est)
        if time.time() - ultimo_asn > 86400:
            ultimo_asn = time.time()
            threading.Thread(target=redes.atualizar, daemon=True).start()
        if est.sujo and time.time() - ultimo_save > SALVAR_A_CADA:
            est.salvar()
            ultimo_save = time.time()
    est.salvar()
    log("[vigia] parado")


if __name__ == "__main__":
    main()
