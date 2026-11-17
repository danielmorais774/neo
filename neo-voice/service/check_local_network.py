"""
Diagnostico de acesso a "Rede Local" (macOS) ao engine do neo-stt.

O macOS bloqueia conexoes a IPs da LAN para binarios sem permissao de Rede Local,
retornando 'Errno 65 No route to host'. Este script testa o python ATUAL.

  python check_local_network.py [host] [port]
"""
import sys
import socket

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.4.185"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 5005

print(f"python: {sys.executable}")
print(f"testando TCP {host}:{port} ...")
try:
    s = socket.create_connection((host, port), timeout=5)
    s.close()
    print("OK: este Python alcanca a LAN (tem permissao de Rede Local).")
except OSError as e:
    print(f"FALHOU: {e}")
    if getattr(e, "errno", None) == 65:
        print("-> Errno 65 = bloqueio de Rede Local do macOS.")
        print("   Conceda em System Settings > Privacy & Security > Local Network,")
        print("   ou rode o serviço com um venv do python do sistema (/usr/bin/python3).")
    sys.exit(1)
