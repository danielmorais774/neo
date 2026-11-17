# neo-whatsapp

Ponte entre o WhatsApp e o neo-core. Mantém a sessão do WhatsApp Web (wppconnect), repassa as mensagens recebidas ao core por webhook e envia as respostas.

## Segurança

- **Lista de permissão.** O Neo só conversa com o número em `ALLOWED_WHATSAPP_PHONE`. Se a variável faltar, o serviço não sobe: não existe número padrão no código.
- **`/notify`** manda mensagem para um destino fixo (`NOTIFY_WHATSAPP_PHONE`). É a única rota exposta para fora, atrás de um nginx que exige `X-Notify-Token` ([`notify-proxy/`](notify-proxy)).
- O resto das rotas só existe na rede interna do Docker.
- Se o WhatsApp Web cair, o cliente reconecta sozinho com backoff, em vez de ficar parado respondendo 503.

## Rotas

| Rota | O que faz |
|---|---|
| `GET /health` | estado da sessão |
| `GET /qr`, `POST /qr/refresh` | pareamento |
| `POST /send` | envia mensagem (só para o número permitido) |
| `POST /notify` | envia para o destino fixo |
| `POST /lookup`, `GET /contacts`, `GET /chats` | busca de contatos e conversas |
| `GET /chats/:jid/messages`, `GET /chats/:jid/media/:msgId` | histórico e mídia |

## Rodar

Sobe pelo `docker-compose.yml` da raiz (`neo-whatsapp` e `notify-proxy`). Precisa de `ALLOWED_WHATSAPP_PHONE`, `NOTIFY_WHATSAPP_PHONE` e `NOTIFY_TOKEN` no `.env`.
