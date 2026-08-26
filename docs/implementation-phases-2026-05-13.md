# Implementação por fases

Data: 2026-05-13

## Fase 0 — Contratos e invariantes
- [x] Introduzido `schema_version` no estado persistido.
- [x] Introduzido `stream_generation` por stream.

## Fase 1 — Worker crash-fast
- [x] Worker em modo single-shot.
- [x] Falha rápida sem loop de recuperação local.

## Fase 2 — Inicialização com parâmetros
- [x] Controller injeta `STREAM_KEY`, `STREAM_GENERATION` e `PROXY_DNS` ao iniciar worker.
- [x] Worker usa `PROXY_DNS` quando disponível.

## Fase 3 — Reconciliador do controller
- [x] Healthcheck periódico de proxy (3s) e worker (3s).
- [x] Worker defeituoso é descartado para substituição.

## Fase 4 — Handover com geração
- [x] `generation` incrementada em handover.
- [x] Start-worker aceita validação opcional de geração.

## Fase 5 — Timeout 3 minutos sem transmissão
- [x] TTL de stream em 180s e release explícito no on_publish_done.

## Fase 6 — Persistência robusta
- [x] Estado inclui `schema_version` e `stream_generation`.
