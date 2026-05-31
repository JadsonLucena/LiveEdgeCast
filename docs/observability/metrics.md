# Controller metrics observability metadata

The controller attaches the same low-cardinality observability metadata to every
controller-owned Prometheus metric and to every structured log line emitted by
`docker/controller/main.py`.

## Allowed metadata labels

Only these labels are propagated to metrics:

| Label | Purpose | Controlled default |
| --- | --- | --- |
| `tenant` | Logical customer or tenant bucket. | `unknown` |
| `environment` | Deployment environment such as `dev`, `stage`, or `prod`. | `unknown` |
| `region` | Deployment region or locality bucket. | `unknown` |

Values are sanitized before use in labels and logs: empty values become
`unknown`, unsupported characters are replaced with `_`, and values are capped at
64 characters. This keeps labels bounded and prevents accidental high-cardinality
metadata from becoming Prometheus labels.

## Precedence

For each allowed label, the controller resolves metadata in this exact order:

1. **HTTP headers**
   - `X-LiveEdgeCast-Tenant`, `X-Tenant`
   - `X-LiveEdgeCast-Environment`, `X-Environment`
   - `X-LiveEdgeCast-Region`, `X-Region`
2. **Query parameters**
   - `tenant` or `metadata_tenant`
   - `environment` or `metadata_environment`
   - `region` or `metadata_region`
3. **Environment variables**
   - `LIVEEDGECAST_TENANT` or `CONTROLLER_METADATA_TENANT`
   - `LIVEEDGECAST_ENVIRONMENT` or `CONTROLLER_METADATA_ENVIRONMENT`
   - `LIVEEDGECAST_REGION` or `CONTROLLER_METADATA_REGION`
4. **Controlled default**
   - `unknown`

The precedence is evaluated independently per field. For example, a request can
supply `X-Tenant` by header while `region` falls back to `LIVEEDGECAST_REGION`.

## Metrics and logs

All controller metrics include the allowed metadata labels in addition to their
original metric-specific labels. Background tasks that do not originate from an
HTTP request use the environment-variable/default branch of the same resolver.

All controller logs are emitted as JSON and include two metadata objects:

- `metadata`: the resolved `tenant`, `environment`, and `region` values.
- `metadata_sources`: the source selected for each field (`header`, `query`,
  `env`, or `default`).

This makes the metadata path auditable while keeping metric labels stable.
