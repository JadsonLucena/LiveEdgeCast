# Controller metrics observability metadata

The controller attaches low-cardinality observability metadata to structured logs
and controlled metadata labels to controller-owned Prometheus metrics emitted by
`docker/controller/main.py`.

## Allowed metadata labels

Only these label names are propagated to metrics. Metric label values are
resolved only from environment variables or the controlled default; header and
query values are log-only.

| Label | Purpose | Controlled default |
| --- | --- | --- |
| `tenant` | Logical customer or tenant bucket. | `unknown` |
| `environment` | Deployment environment such as `dev`, `stage`, or `prod`. | `unknown` |
| `region` | Deployment region or locality bucket. | `unknown` |

Blank values are ignored during precedence resolution so the resolver can fall
back to the next source. Values are sanitized before use in labels and logs:
empty resolved values become `unknown`, unsupported characters are replaced with
`_`, and values are capped at 64 characters. This keeps labels bounded and
prevents accidental high-cardinality metadata from becoming Prometheus labels.

## Structured log metadata precedence

For each allowed label in structured logs, the controller resolves metadata in this exact order:

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

Controller logs emitted by `docker/controller/main.py` are JSON-formatted and
include two metadata objects:

- `metadata`: the resolved `tenant`, `environment`, and `region` values using
  the full precedence above.
- `metadata_sources`: the source selected for each field (`header`, `query`,
  `env`, or `default`).

Controller metrics include the allowed metadata label names in addition to their
original metric-specific labels, but metric label values never come from HTTP
headers or query parameters. Metrics use only the controlled configuration branch
(environment variables) and then the controlled default (`unknown`). This prevents
request-controlled values from creating unbounded Prometheus time series while
logs remain auditable for request-supplied metadata. If your log backend
automatically indexes JSON fields, do not promote request-sourced `metadata.*`
values (`metadata_sources` of `header` or `query`) to labels/indexes without an
allowlist.
