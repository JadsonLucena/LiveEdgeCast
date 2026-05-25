# Revisão do fluxo atual (Controller, Proxy, Worker)

Data: 2026-05-13

## Conclusão rápida

O projeto já implementa parte importante da arquitetura **pull-only** (Proxy recebe publish, Worker faz pull do Proxy e push para YouTube), com persistência de estado no Controller e monitoramento básico. Porém, ainda existem diferenças relevantes em relação ao fluxo desejado e aos guard rails descritos.

## O que já está alinhado

1. **Pull-only entre Worker e Proxy**
   - O Proxy notifica o Controller no `on_publish_start` com `stream` e `proxy_pod`.
   - O Worker consulta o Controller para descobrir `proxyDns` e `youtubeKey`, depois faz `ffmpeg -i rtmp://proxy/live/stream -> rtmp://youtube/live2/key`.

2. **Mapeamento no Controller com stream -> proxy e stream -> worker**
   - O Controller mantém `stream_to_worker`, `worker_to_stream`, `stream_to_proxy` e `stream_registry`.

3. **Persistência de estado do Controller**
   - O estado crítico é salvo/restaurado em ConfigMap (`controller-state`).

4. **Observabilidade inicial**
   - Existem métricas Prometheus para proxy/worker/stream e endpoints `/health` e `/stats` em Proxy/Worker.

5. **Sem heartbeat de Worker/Proxy por padrão de aplicação**
   - Worker/Proxy não expõem heartbeat customizado; saúde é por HTTP endpoints.

## Diferenças/gaps frente ao fluxo desejado

1. **Timeout de 3 minutos sem cliente no Proxy**
   - Atualmente o encerramento depende principalmente do `on_publish_done` do NGINX + reconciliação de saúde no Controller.
   - Regra exata “3 minutos sem transmitir => Proxy informa Controller para remover Worker” precisa ser consolidada como contrato explícito (evitar depender de caminhos indiretos).

2. **Worker deve crashar em até 5s sem conseguir consumir**
   - Hoje o `worker_recovery_loop.sh` faz retry por até 8 tentativas/120s. Isso conflita com a estratégia “let’s crash rápido”.

3. **FFmpeg iniciar com o servidor/pod (startup args)**
   - Hoje o Worker só inicia FFmpeg após chamada `/streams/started` via Controller.
   - Requisito pede startup imediato com argumentos de origem/destino no pod.

4. **Health checks centralizados no Controller a cada 3s (proxy e worker)**
   - Há healthcheck de proxy e coleta de métricas, mas a responsabilidade e frequência exata de 3s para ambos (proxy + worker) precisa ficar estrita e uniforme.

5. **Failover de worker**
   - Existe recuperação/retry no próprio worker, mas a regra deseja que o Controller descarte worker defeituoso e substitua. Hoje a autocorreção local do worker pode atrasar esse ciclo.

6. **Migração de stream entre pods de Proxy**
   - Existem mecanismos de handover, mas a política pode ser simplificada para evitar trabalho duplicado: se troca de proxy inevitavelmente derruba consumo, priorizar recriação limpa do worker com estado atualizado.

7. **URL base de ingest do YouTube em variável de ambiente Kubernetes**
   - Hoje o script usa URL fixa `rtmp://a.rtmp.youtube.com/live2/` no worker.

8. **Guard rail de unicidade de stream key simultânea**
   - Existem sinais de controle por owner/proxy no registro, mas a garantia precisa ser explícita e transacional (uma única stream ativa por chave em qualquer condição de corrida).

## Regras adicionais relevantes (que faltaram no pedido e valem incluir)

1. **Contrato de idempotência entre eventos**
   - `on_publish_start`, `on_publish_done`, `allocate`, `release`, recriação de worker e handover devem ser idempotentes (repetições e reordenação são normais em ambiente distribuído).

2. **Versionamento do estado persistido**
   - Salvar `schema_version` no ConfigMap de estado para permitir evolução segura sem quebrar recuperação.

3. **Leases/geração para evitar split-brain de workers**
   - Associar `generation` (ou token) por stream no mapeamento; Worker só opera se token atual bater, evitando worker antigo continuar após failover.

4. **Política de término explícita para worker órfão**
   - Se `stream` não existir mais no mapeamento, o worker deve encerrar imediatamente (crash) para acelerar convergência.

5. **Timeouts e circuit-breakers de chamadas internas**
   - Toda chamada HTTP Controller->Proxy/Worker e scripts shell deve ter timeout curto e tratamento explícito para evitar bloqueio em cascata.

6. **Reconciliação periódica com Kubernetes API**
   - Além de `/health` e `/stats`, conferir existência real do pod e fase (`Running/Ready`) para detectar inconsistências entre estado lógico e estado real.

7. **Política de observabilidade por labels estáveis**
   - Métricas segmentadas por `stream_key`, `proxy_pod`, `worker_pod`, `session_id` para correlação em reassign/handover.

8. **Backoff controlado no Controller (não no Worker)**
   - Para manter “let’s crash”, o retry deve migrar para Controller com limite e jitter; Worker deve falhar rápido e sair.

## Direção recomendada de implementação

1. Tornar Worker “single-shot”: falhou consumo por >5s => exit != 0.
2. Controller passa `STREAM_KEY`, `PROXY_DNS`, `YOUTUBE_BASE_URL` e token de geração via env/args ao criar pod.
3. Remover loop de recuperação longa dentro do worker.
4. Controller executa reconciliador único (3s) para:
   - saúde de proxies do mapeamento;
   - saúde/transmissão de workers do mapeamento;
   - recriação/substituição quando necessário.
5. Garantir unicidade da stream key com lock + geração + persistência atômica.
6. Externalizar URL de YouTube para ConfigMap/Env (`RTMP_PUSH_BASE_URL`).

