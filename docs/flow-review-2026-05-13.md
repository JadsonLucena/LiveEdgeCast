# Revisão do fluxo (Controller, Proxy, Worker)

Data original: 2026-05-13

> Esta revisão foi substituída pela arquitetura atual descrita em
> `stream-lifecycle-and-reconciliation.md`.

O Proxy recebe o publish sem enviar callbacks de início ou fim ao Controller.
O ciclo de vida de Worker não faz parte desse fluxo.

## Limites atuais

- O Proxy expõe os endpoints de saúde e estatísticas.
- O Controller expõe apenas `/health`.
- Não existem eventos ou endpoints internos de alocação e remoção de Worker.
- Não há reconciliação de Proxy ou Worker no Controller.

Consulte `stream-lifecycle-and-reconciliation.md` para o contrato atual dos
endpoints e a divisão de responsabilidades.
