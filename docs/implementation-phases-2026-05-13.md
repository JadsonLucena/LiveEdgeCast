# Implementação por fases

Data original: 2026-05-13

Este plano histórico foi substituído pela arquitetura atual. O Proxy não envia
callbacks de início ou fim ao Controller, e o fluxo não inclui alocação,
handover, recuperação ou reconciliação de Worker. Uma fase posterior poderá
introduzir recursos declarativos `LiveStream`.

Consulte `stream-lifecycle-and-reconciliation.md` para a descrição vigente.
