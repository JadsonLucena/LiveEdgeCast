# Plano de testes experimentais

Cenários: 1,5,10,15,20 streams; piloto incremental até saturação; falha de worker; falha de proxy; reconexão e duplicata de streamKey.

Variáveis independentes: concorrência, bitrate, duração, tipo de falha.
Variáveis dependentes: latências (cold start/ready), taxa sucesso alocação, handover, MTTR, recursos CPU/mem/rede.
Controles: cluster, versão imagens, namespace, target RTMP, janela de coleta.
Repetições: mínimo 10 por cenário.
Dados coletados: métricas Prometheus + logs JSON do controller.
Critérios de aceitação: disponibilidade > 99%, idempotência preservada, limpeza de órfãos funcional.
Limitações: alguns timestamps dependem de eventos observáveis (e não do kernel/runtime exato).
