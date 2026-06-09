# Plano experimental do LiveEdgeCast

Este plano define como medir cold start, alocação, handover, recuperação e uso
de recursos do LiveEdgeCast de forma repetível. Ele deve ser usado em conjunto
com o catálogo de métricas e as consultas PromQL de `docs/observability`.

## Objetivos

1. Medir a distribuição de cold start de stream do primeiro evento observado no
   proxy até o primeiro progresso do FFmpeg.
2. Quantificar confiabilidade do plano de controle: sucesso de registro,
   alocação, criação de worker, release e idempotência.
3. Avaliar handover/failover quando o owner proxy ou o worker falha.
4. Medir MTTR de worker e impacto na saúde do pipeline.
5. Relacionar carga ativa com CPU, memória e rede por componente.
6. Documentar limitações de observabilidade e ameaças à validade dos resultados.

## Hipóteses e métricas primárias

| Hipótese | Métrica primária | Consulta base |
| --- | --- | --- |
| H1: o sistema inicia streams dentro do orçamento definido. | P50/P95/P99 de `stream_lifecycle_phase_seconds{phase="proxy_to_first_progress"}`. | `histogram_quantile()` sobre buckets do histograma. |
| H2: a alocação é confiável sob carga. | `stream_allocation_total{status="success"} / stream_allocation_total`. | Taxa de sucesso de alocação. |
| H3: handover preserva ownership sem limpar stream ativo de outro proxy. | `stream_proxy_handover_total`, `handover_conflict_total`, `stale_ended_events_ignored_total`. | Taxa de handover e conflitos. |
| H4: recuperação de worker tem MTTR aceitável. | P95 e média de `worker_recovery_duration_seconds` para tentativas de recovery, acompanhados de `worker_recovery_total{status="success",reason="replaced"}`. | MTTR por histograma e taxa de sucesso por contador. |
| H5: uso de recursos cresce de forma proporcional à carga. | CPU, memória e rede por componente via cAdvisor/kubelet. | Agregações por `component`. |

## Variáveis experimentais

### Variáveis independentes

| Variável | Níveis sugeridos | Observações |
| --- | --- | --- |
| Número de streams concorrentes | `1`, `5`, `10`, `25`, `50` | Ajustar ao tamanho do cluster para evitar saturação irreversível. |
| Taxa de chegada de streams | baixa: 1 stream/30s; média: 1 stream/10s; rajada: todos simultâneos | Afeta concorrência de criação de Pods e API Kubernetes. |
| Duração do stream | curta: 2 min; média: 10 min; longa: 30 min | Streams longos ajudam a observar estabilidade e recursos. |
| Cenário de falha | sem falha, falha de worker, falha de proxy owner, reinício do controller | Executar isoladamente para atribuição causal. |
| Estado inicial | cluster frio, imagens pré-puxadas, controller recém-reiniciado | Cold start deve ser reportado separado por estado inicial. |
| Recursos de worker/proxy | requests/limits padrão, reduzidos, ampliados | Usar apenas se o objetivo incluir sensibilidade a recursos. |
| Número de réplicas de proxy | mínimo, nominal, sob escala | Afeta distribuição de streams e handover. |
| Metadados controlados | `environment`, `region`, `tenant` | Mantê-los fixos durante uma repetição para agregações estáveis. |

### Variáveis dependentes

- Cold start P50/P95/P99 fim-a-fim e por fase.
- Taxa de sucesso de alocação e criação de worker.
- Tempo de registro, alocação, criação de Pod, readiness e primeiro progresso.
- Handover rate, success rate e conflict rate.
- MTTR médio/P95 de worker.
- Número de workers ativos, proxies ativos, publishers e clientes RTMP.
- Órfãos aproximados e eventos de exclusão de worker sem stream associado.
- CPU, memória e rede por componente.
- Erros de scrape, watch e exporter.

### Variáveis controladas

- Versão do código, imagem dos containers e manifests Kubernetes.
- Tamanho e tipo dos nós do cluster.
- Versão do Kubernetes, CNI, runtime de container e stack Prometheus.
- Fonte de mídia/bitrate/resolução e comando FFmpeg.
- Região/localidade de execução e latência entre gerador de carga e cluster.
- Janela de retenção/scrape do Prometheus e intervalo de scrape.
- Configurações de healthcheck e timeouts do controller.

## Desenho experimental

### Cenário A: baseline de cold start

1. Preparar cluster sem streams ativos e confirmar que Prometheus está coletando
   controller, proxy e worker quando existirem workers.
2. Iniciar uma sequência de streams com taxa controlada.
3. Manter cada stream ativo até observar `t_ffmpeg_first_progress` e pelo menos
   60 segundos adicionais de progresso FFmpeg saudável.
4. Encerrar streams e aguardar release/cleanup.
5. Coletar percentis de cold start, decomposição por fase e recursos.

### Cenário B: carga concorrente

1. Repetir o baseline com níveis crescentes de concorrência.
2. Para cada nível, executar uma rampa gradual e uma rajada simultânea.
3. Registrar saturation symptoms: aumento de scheduling, falhas de API, queda de
   `proxy_rtmp_stats_up`, staleness de FFmpeg ou aumento de conflitos.
4. Comparar percentis com o baseline de `1` stream.

### Cenário C: falha de worker e MTTR

1. Iniciar streams até workers ficarem Ready e FFmpeg saudável.
2. Induzir uma falha de saúde do worker visível ao controller por repetição,
   preferencialmente derrubando/reiniciando o container do worker ou fazendo o
   endpoint `/health` servido pelo nginx falhar conforme permitido pelo ambiente.
   Não use apenas congelamento do FFmpeg como método primário de MTTR: o
   controller consulta `/health` e não `worker_ffmpeg_health_state`, então um
   nginx saudável pode mascarar o congelamento do processo FFmpeg. Também não use
   deleção direta do Pod como método primário de MTTR: na implementação atual, a
   ausência do Pod impede acumular falhas de healthcheck até o threshold que
   observa `worker_recovery_duration_seconds`.
3. Medir detecção via `worker_healthcheck_total`, recuperação via
   `worker_recovery_duration_seconds` e retorno de `worker_ffmpeg_health_state`.
4. Confirmar que a alocação final aponta para o worker substituto e que o antigo
   foi removido. Trate deleção de Pod como cenário separado de disrupção/orfandade
   até o controller registrar recovery para esse caminho.

### Cenário D: falha de proxy owner e handover

1. Iniciar stream em proxy owner conhecido.
2. Tornar o owner inelegível, por exemplo removendo o Pod ou fazendo healthcheck
   falhar.
3. Reenviar/publicar pelo proxy candidato e observar `stream_proxy_handover_total`.
4. Verificar que eventos `ended` obsoletos são ignorados e que conflitos são
   registrados quando o owner original ainda está saudável.
5. Coletar tempo até worker recriado/apontado para o novo proxy.

### Cenário E: restart do controller e reconciliação

1. Iniciar streams e confirmar estado persistido.
2. Reiniciar o controller.
3. Verificar recuperação de estado, idempotência de eventos e ausência de cleanup
   indevido.
4. Medir replays, erros de watch, orphans aproximados e estabilidade das métricas.

## Repetições e randomização

- Executar no mínimo **30 repetições válidas** por combinação principal de
  cenário e nível de carga para estimar mediana e P95 inicial. Para P99, exigir
  volume substancialmente maior de amostras de stream por combinação (centenas ou
  milhares, conforme a precisão desejada) ou reportar P99 apenas como exploratório
  junto com máximo observado e intervalo de confiança amplo. Para smoke tests ou
  regressões rápidas, usar no mínimo 5 repetições e não reportar P99.
- Randomizar a ordem dos níveis de carga dentro de cada bloco diário para reduzir
  viés de aquecimento do cluster e ruído externo.
- Separar cenários de falha: não misturar falha de worker e proxy na mesma
  repetição, exceto em testes exploratórios não usados para resultados primários.
- Usar um `experiment_id` fixo por campanha e `run_id` único por repetição nos
  logs/requests, mantendo `tenant`, `environment` e `region` constantes.
- Descartar uma repetição somente com critério pré-definido: falha do gerador de
  carga, Prometheus indisponível durante a janela principal ou `worker_pod_lifecycle_watch_up=0`
  durante medição de cold start.

## Procedimento de preparação

1. Registrar commit, tag das imagens, manifests aplicados e versão do cluster.
2. Verificar targets Prometheus `UP` para controller e proxies; workers podem não
   existir antes da primeira stream.
3. Limpar streams residuais, workers órfãos e port-forwards antigos.
4. Sincronizar relógios dos nós quando o ambiente permitir; registrar fonte NTP.
5. Pré-carregar imagens se o cenário não for explicitamente de cluster frio.
6. Fixar requests/limits e número inicial de réplicas para a campanha.
7. Definir janelas Prometheus: scrape interval, evaluation interval e retenção.

## Coleta de dados

Para cada repetição, armazenar:

- Parâmetros: cenário, nível de carga, taxa de chegada, duração, estado inicial,
  recursos e número de réplicas.
- Timestamps de início/fim da repetição em UTC.
- Export Prometheus ou snapshots das consultas em `promql.md`.
- Logs estruturados do controller correlacionados por `stream`/prefixo do
  stream key e pelos artefatos do gerador de carga. Use `experiment_id` e `run_id`
  apenas quando esses campos forem propagados por headers/query parameters ou
  configurados no ambiente do controller; os hooks RTMP normais não os enviam.
- Eventos Kubernetes relevantes dos namespaces `media` e `monitoring`.
- Resultado do gerador de carga: streams iniciados, encerrados, falhas e bitrate.

## Critérios de aceitação

### Validade da repetição

Uma repetição é válida quando todos os critérios abaixo forem verdadeiros:

- O Prometheus coletou controller durante toda a janela principal.
- `proxy_rtmp_stats_up` permaneceu `1` para proxies participantes ou a falha de
  scrape foi explicitamente o objeto do cenário.
- `worker_pod_lifecycle_watch_up` permaneceu `1` durante medições de cold start.
- O número de streams observado em `proxy_rtmp_active_streams` corresponde ao
  plano de carga dentro de tolerância de uma janela de scrape.
- Não houve mudança não planejada de manifests, escala base ou imagens.
- Eventos de início/fim são correlacionáveis com a repetição por stream key,
  artefatos do gerador de carga ou metadados de log explicitamente propagados;
  não rejeite runs RTMP normais apenas porque `run_id` aparece como `unknown` nos
  logs do controller.

### Aceitação funcional

- Allocation success ≥ 99% em cenários sem falha induzida, excluindo replays
  idempotentes do denominador primário.
- Nenhum cleanup indevido de stream pertencente a outro proxy durante handover.
- Workers substitutos são criados e workers antigos removidos em falhas de worker.
- Sem crescimento persistente de workers aproximados como órfãos após a janela de
  cleanup.
- Exporters de worker não apresentam crescimento relevante de
  `worker_ffmpeg_exporter_errors_total`.

### Aceitação de desempenho

Os limiares numéricos devem ser definidos antes da campanha conforme ambiente.
Modelo recomendado:

| Métrica | Limiar a preencher antes da execução |
| --- | --- |
| Cold start P95 `proxy_to_first_progress` | `<= X s` |
| Cold start P99 `proxy_to_first_progress` | `<= Y s` |
| MTTR P95 de worker | `<= Z s` |
| Handover conflict rate em owner saudável | `<= A%` |
| Erro de alocação sem falha induzida | `<= B%` |
| CPU média por componente no nível nominal | `<= C cores` |
| Memória P95 por componente no nível nominal | `<= D GiB` |

Não altere limiares após observar resultados; se forem recalibrados, documente a
campanha como exploratória e execute nova campanha confirmatória.

## Análise estatística

- Reportar mediana, P95, P99, média, desvio padrão e intervalo de confiança
  bootstrap para métricas de latência quando houver amostras suficientes.
- Usar gráficos de distribuição ou boxplots por cenário e nível de carga; evitar
  apenas médias para cold start.
- Reportar denominador de cada taxa e quantidade de amostras excluídas por
  limitação de observabilidade; para lifecycle, separar fases observadas/ignoradas
  de timestamps que nunca chegaram e, portanto, não incrementam o contador de
  observações de fase.
- Comparar cenários por diferença relativa ao baseline e diferença absoluta em
  segundos/pontos percentuais.
- Para P99, exigir volume de amostras compatível; com poucas dezenas de amostras,
  reportar P95 e máximo observado, deixando P99 como exploratório. Distinguir
  repetições de amostras de stream e considerar agrupamento por repetição quando
  múltiplos streams forem gerados no mesmo run.

## Ameaças à validade

### Validade interna

- Variações de scheduling Kubernetes, pull de imagem, cache de nós e pressão de
  recursos podem explicar parte do cold start independentemente do LiveEdgeCast.
- Replays idempotentes podem inflar taxas de sucesso se misturados com criações
  reais; separar por `reason`.
- Falhas induzidas manualmente podem não representar falhas reais de rede,
  kernel, CNI ou aplicação.
- Healthchecks têm intervalos, jitter e thresholds; MTTR observado inclui atraso
  de detecção, não apenas tempo de substituição.
- Prometheus usa scraping periódico; eventos entre scrapes podem ser agregados ou
  observados com atraso.

### Validade externa

- Resultados dependem do provedor Kubernetes, tipo de nó, CNI, registry, distância
  do gerador de carga e configuração RTMP/FFmpeg.
- Bitrate, resolução e perfil de codificação escolhidos podem não representar
  workloads de produção.
- Escalas pequenas podem esconder gargalos de API server, Prometheus ou rede que
  aparecem em produção.
- Escalas muito altas em ambiente de teste podem criar gargalos artificiais não
  existentes em clusters dimensionados para produção.

### Validade de construto

- `proxy_to_first_progress` aproxima experiência de cold start, mas primeiro
  progresso FFmpeg não é exatamente primeiro frame entregue ao consumidor.
- `worker_recovery_duration_seconds` mede a tentativa de recuperação do
  controller, não necessariamente recuperação percebida por todos os clientes.
- Órfãos não têm métrica direta; aproximações por Pods vivos menos atividade podem
  confundir workers em inicialização, encerramento ou scrape atrasado.
- Métricas por Pod mudam com rollouts e nomes gerados; agregações por componente
  são mais estáveis para conclusões do artigo.

### Validade de conclusão

- Poucas repetições tornam P99 instável; evitar conclusões fortes sem amostras
  suficientes.
- Comparações entre cenários precisam controlar ordem de execução e aquecimento.
- Múltiplas métricas aumentam risco de cherry-picking; registrar hipóteses e
  métricas primárias antes da execução.
- Outliers devem ser reportados e explicados, não removidos sem critério prévio.

## Limitações de observabilidade

- Timestamps de proxy e controller podem estar em relógios diferentes; durações
  negativas são descartadas e contam como `reason="negative_duration"`.
- Alguns endpoints de lifecycle são aproximações documentadas: início de FFmpeg é
  notificação do worker antes do launch, e primeiro progresso depende da cadência
  de emissão do FFmpeg.
- Kubernetes não expõe diretamente conclusão exata de pull de imagem nem
  `execve` do processo de usuário na instrumentação atual.
- O modelo de lifecycle no controller é em memória e limpo no release; Prometheus
  e logs são as saídas duráveis.
- Histograms Prometheus não permitem corrigir observações antigas aproximadas;
  por isso fases com endpoints aproximados são puladas no histograma canônico.
- Métricas do worker podem desaparecer quando o Pod é deletado; use Prometheus
  com retenção e scrape interval adequados para capturar janelas curtas.
- Métricas de recurso emitidas pelo controller podem ser aproximações; para o
  artigo, cAdvisor/kubelet deve ser a fonte primária de CPU, memória e rede.
- Logs estruturados contêm `stream`, `generation`, `proxy_pod` e `worker_pod`, mas
  esses identificadores não são labels Prometheus por controle de cardinalidade.
- Não há métrica Prometheus direta de órfãos; exclusões precisam ser observadas
  por logs ou inferidas por comparação entre Pods e atividade.
- Fases de lifecycle com endpoint ausente não incrementam
  `stream_lifecycle_phase_observations_total`; compare timestamps individuais
  observados com a carga planejada antes de interpretar perdas de cold start.

## Checklist por repetição

1. Confirmar targets Prometheus e relógio da janela.
2. Registrar parâmetros e `run_id`.
3. Executar carga ou falha induzida.
4. Aguardar estabilização e cleanup.
5. Exportar consultas primárias e logs.
6. Validar critérios de repetição válida.
7. Marcar repetição como válida, inválida ou exploratória com justificativa.
