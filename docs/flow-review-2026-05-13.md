# Revisão do fluxo (Controller, Proxy, Worker)

Data original: 2026-05-13

> Esta revisão foi substituída pela arquitetura de notificações descrita em
> `stream-lifecycle-and-reconciliation.md`.

O Proxy recebe o publish e envia eventos de início e fim ao Controller. Esses
eventos são transitórios: o Controller apenas os registra no log e responde à
requisição. O ciclo de vida de Worker não faz parte desse fluxo.

## Limites atuais

- O Proxy expõe os endpoints de saúde e estatísticas.
- O Controller expõe `/health`, `/streams/started` e `/streams/ended`.
- Os eventos não causam alocação, substituição ou remoção de Worker.
- Não há reconciliação de Proxy ou Worker no Controller.

Consulte `stream-lifecycle-and-reconciliation.md` para o contrato atual dos
endpoints e a divisão de responsabilidades.
