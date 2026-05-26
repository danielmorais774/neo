# PKS first-message test

Este fluxo testa o caso de primeira interacao:

`Oi, meu nome e Daniel Morais`

Objetivo:
- usar a rota real `/api/v1/desktop/chat`
- forcar `provider=deepseek` e `model=deepseek-chat`
- isolar tudo em um banco Docker separado
- replicar a tabela `user` do banco principal para reaproveitar credenciais
- conferir com `SELECT` todas as tabelas `pks_*`

## Ambiente

O `docker-compose.yml` agora tem um profile `pks-test` com:
- `neo-postgres-pks-test`
- `neo-core-pks-test`

Esse ambiente usa:
- Postgres separado
- mesmas credenciais (`neo` / `neo`)
- mesma API do `neo-core`, mas exposta em `http://127.0.0.1:8001`

## Comandos

Subir ambiente:

```bash
cd neo-core
make pks-test-up
```

Rodar o teste completo:

```bash
cd neo-core
make pks-test-run
```

Rodar contra um usuario especifico:

```bash
cd neo-core
./scripts/pks_first_message_test.sh run --email daniel@exemplo.com
```

Ou por `user_id`:

```bash
cd neo-core
./scripts/pks_first_message_test.sh run --user-id 11111111-1111-1111-1111-111111111111
```

Derrubar ambiente e apagar volume:

```bash
cd neo-core
make pks-test-down
```

## O que o script faz

1. sobe `neo-postgres-pks-test` e `neo-core-pks-test`
2. reseta o schema do banco de teste
3. roda `alembic upgrade head`
4. replica os registros da tabela `user` do banco principal
5. escolhe um usuario de teste
6. envia a primeira mensagem para `/api/v1/desktop/chat`
7. imprime `SELECT *` de todas as tabelas `pks_*`
8. valida:
   - 1 workspace ativo do usuario
   - workspace com nome `Daniel Morais`
   - 7 spaces base
   - 1 entity primaria em `Perfil`
   - nenhuma duplicata `Daniel Morais` em `Pessoas`
   - field `full_name = Daniel Morais`

## Observacao

Se voce quiser testar outra frase, rode:

```bash
cd neo-core
TEST_MESSAGE="Oi, meu nome e Daniel Morais" ./scripts/pks_first_message_test.sh run --email daniel@exemplo.com
```
