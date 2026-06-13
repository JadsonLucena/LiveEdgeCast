# Formato do relatório experimental

Cada execução cria:

```text
reports/<experiment-id>/
  metadata.json
  raw/
  metrics/
  logs/
  charts/
  report.md
  report.json
```

Os CSVs em `metrics/` são a base para tabelas e discussão do artigo. Os arquivos em `raw/` preservam evidências brutas para auditoria e reprocessamento.
