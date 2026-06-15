# LiveEdgeCast run_experiment.py rollout wait fix

This patch updates `tools/experiments/run_experiment.py` so that, when `--patch-proxy-context` is used, the runner waits after applying and restoring context for:

1. Kubernetes Deployment rollout status;
2. controller `/health` through `--controller-url`;
3. healthy Prometheus targets for `controller` and `proxy`;
4. scoped controller Prometheus samples after patching context.

Replace your project file with:

```bash
cp tools/experiments/run_experiment.py tools/experiments/run_experiment.py.bak
cp /path/to/this/tools/experiments/run_experiment.py tools/experiments/run_experiment.py
python3 -m py_compile tools/experiments/run_experiment.py
```
