# Fluxo de Ciclo de Vida e Reconciliação de Streams

Este documento descreve o **comportamento atual** do sistema com base no código do controller/proxy/worker e no diagrama `diagrams/activity-flow.mmd` (fonte da verdade).

## Fluxo padrão

### Início do Ciclo de Vida (Início da Publicação)
- O cliente inicia a publicação RTMP no load balancer.
- O load balancer encaminha o stream para o proxy menos utilizado (estratégia de balanceamento).
- O proxy executa `on_publish_start.sh` via `exec_publish`.
- O proxy notifica o controller em `POST /streams/started?stream=<STREAM_KEY>&proxy_pod=<PROXY_POD>`.
- O controller registra ownership da stream e persiste estado.
- O controller garante idempotência:
  - se já existir worker para a stream, retorna `idempotent_replay`;
  - se não existir, cria novo worker com `create_namespaced_pod`.
- Na criação do worker, o controller injeta as variáveis de ambiente:
  - `STREAM_KEY`
  - `PROXY_DNS`
- O controller atualiza e persiste mapeamentos internos de alocação (equivalente lógico):

```json
{
  "streams": {
    "<STREAM_KEY>": {
      "proxyPod": "string",
      "workerPod": "string"
    }
  }
}
```

> Implementação atual do estado usa dicionários normalizados (`stream_registry`, `stream_to_proxy`, `stream_to_worker`, `worker_to_stream`, `stream_generation`) em vez de um único objeto `streams`.

- O worker executa `entrypoint.sh`, que inicia `worker_stream_runner.sh` e `nginx -g 'daemon off;'`.
- O worker faz pull do proxy e push para o destino RTMP.

### Fim do Ciclo de Vida (Fim da Publicação)
- O cliente termina a publicação RTMP.
- Após 60s de inatividade do publisher (`drop_idle_publisher 60s`), o proxy executa `on_publish_done.sh`.
- O proxy notifica o controller em `POST /streams/ended?stream=<STREAM_KEY>&proxy_pod=<PROXY_POD>`.
- O controller libera o worker.
- O controller remove mapeamentos internos da stream (ownership/alocação/geração) e persiste estado.

## Fluxo de Reconciliação

Deve haver **somente um worker por `streamKey`**. A alocação é idempotente e protegida por lock no controller.

### Reconciliação de Saúde do Proxy
- A verificação só começa quando o proxy está `Ready`.
- Há delay pós-ready antes de contar falhas (`warming_up`).
- Se o proxy estiver unhealthy/deletado, a saúde falha.
- O controller deleta worker e expira ownership da(s) stream(s) após **3 falhas consecutivas** de healthcheck do proxy.
- As tentativas rodam a cada **3s** com jitter de **1.5s**.
- O healthcheck de worker só é executado quando o proxy owner está `healthy`.

### Reconciliação de Saúde do Worker
- A verificação só começa quando o worker está `Ready`.
- Há delay pós-ready antes do primeiro `/health`.
- O controller recria worker após **3 falhas consecutivas** de healthcheck do worker.
- As tentativas rodam a cada **3s** com jitter de **1.5s**.
- A contagem de falhas é atrelada ao **UID do worker**, reiniciando quando há troca de UID (auto-restart do Kubernetes, recreate ou handover).
- O healthcheck do worker só ocorre quando o proxy owner está `healthy`.
- O worker pode falhar por timeout RTMP do FFmpeg (~5s de `-rw_timeout`) e ser reiniciado automaticamente pelo Kubernetes até convergência.

## Regras de Handover
- O proxy/nginx não deve permitir uso simultâneo da mesma stream key no mesmo pod (configuração `live on` no `nginx.conf`).
- Se não houver mapeamento para a `streamKey`, o controller cria novo mapeamento.
- Quando a `streamKey` já está em uso, o controller avalia elegibilidade de handover.
- A troca de ownership (`proxyPod`) só ocorre se o proxy owner anterior estiver `unhealthy` ou deletado.
- Em handover aceito, o controller incrementa `generation` e pode substituir o worker para atualizar `PROXY_DNS`.

## Limpeza
- A cada **60s**, o sweeper verifica workers órfãos.
- Antes de deletar, revalida em lock (double-check) para evitar corrida.
- Se continuar órfão, remove o pod.

## Limites de Responsabilidade

- **Proxy**:
  - notificar início (`/streams/started`)
  - notificar fim (`/streams/ended`) após timeout de 60s de idle publisher
  - não tomar decisões de alocação/reconciliação

- **Worker**:
  - executar pull/push para o fluxo atribuído
  - falhar rapidamente em problemas críticos de execução

- **Controller**:
  - orquestrar ciclo de vida
  - executar healthchecks e reconciliação
  - persistir estado e garantir segurança de geração

## Observação sobre TTL

No fluxo atual, não há TTL temporal por `lastSeen` aplicado no controller. A “expiração” vigente é dirigida por falhas consecutivas de healthcheck do proxy (threshold de 3 falhas).
