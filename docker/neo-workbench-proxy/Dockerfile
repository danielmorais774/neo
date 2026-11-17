# neo-workbench-proxy — proxy de saída (egress) do sandbox do Neo.
#
# O sandbox (neo-workbench) fica numa rede `internal: true` (sem rota externa
# nenhuma — nem internet, nem LAN). A ÚNICA saída dele é este proxy, que:
#   - tem internet (está numa bridge normal), e
#   - RECUSA destinos privados (RFC1918/LAN) via lista de filtro.
# Resultado: "internet sim, LAN não", 100% em docker-compose, sem iptables no host
# e sobrevivendo a reboot. Ver docs/workbench-background-jobs-plan.md.

FROM alpine:3.20

RUN apk add --no-cache tinyproxy

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY filter /etc/tinyproxy/filter

EXPOSE 8888

# -d: foreground (não daemoniza) para o Docker gerenciar o processo.
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
