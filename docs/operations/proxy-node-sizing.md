# Proxy node sizing

The proxy Deployment keeps lightweight container resource requests/limits because
those values describe the proxy Pod containers, not the Kubernetes node capacity.
To run proxy Pods on a node with a 2 vCPU / 2 GiB RAM budget, provision that node
through the cluster provider or local runtime and then constrain scheduling to it.

Recommended operational steps:

1. Create or select a Kubernetes node with 2 vCPU and 2 GiB RAM using the
   infrastructure layer that owns node capacity (for example the cloud node pool,
   VM size, Docker/kind node container limits, or local cluster runtime).
2. When deploying with `tools/up.sh`, optionally let the script label the proxy
   node, apply local Docker limits when the node is a Docker/kind container, and
   pin the proxy Deployment to that label:

   ```bash
   PROXY_NODE_NAME=<proxy-node-name> \
   LIMIT_PROXY_NODE_RESOURCES=true \
   PATCH_PROXY_NODE_SELECTOR=true \
   ./tools/up.sh
   ```

   The defaults are `PROXY_NODE_CPUS=2`, `PROXY_NODE_MEMORY=2g`,
   `PROXY_NODE_LABEL_KEY=liveedgecast.io/node-role`, and
   `PROXY_NODE_LABEL_VALUE=proxy`.

3. To do the same steps manually, label that node as the proxy node:

   ```bash
   kubectl label node <proxy-node-name> liveedgecast.io/node-role=proxy
   ```

4. If the deployment should be pinned to that node, apply a scheduling constraint
   as an environment-specific overlay or patch rather than changing the base
   `k8s/proxy-deployment.yaml` resource requests:

   ```bash
   kubectl patch deployment proxy -n media --type merge \
     -p '{"spec":{"template":{"spec":{"nodeSelector":{"liveedgecast.io/node-role":"proxy"}}}}}'
   ```

Keep the base proxy Deployment resources small unless you specifically want to
reserve that much CPU/memory per proxy Pod. Node sizing and Pod resource sizing
are separate controls in Kubernetes.
