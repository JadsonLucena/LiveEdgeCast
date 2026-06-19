# Proxy node sizing

The proxy Deployment keeps lightweight container resource requests/limits because
those values describe the proxy Pod containers, not the Kubernetes node capacity.
To run proxy Pods on a node with a 2 vCPU / 2 GiB RAM budget, provision that node
through the cluster provider or local runtime and then constrain scheduling to it.

Recommended operational steps:

1. Create or select a Kubernetes node with 2 vCPU and 2 GiB RAM using the
   infrastructure layer that owns node capacity (for example the cloud node pool,
   VM size, Docker/kind node container limits, or local cluster runtime).
2. Label that node as the proxy node:

   ```bash
   kubectl label node <proxy-node-name> liveedgecast.io/node-role=proxy
   ```

3. If the deployment should be pinned to that node, apply a scheduling constraint
   as an environment-specific overlay or patch rather than changing the base
   `k8s/proxy-deployment.yaml` resource requests:

   ```bash
   kubectl patch deployment proxy -n media --type merge \
     -p '{"spec":{"template":{"spec":{"nodeSelector":{"liveedgecast.io/node-role":"proxy"}}}}}'
   ```

Keep the base proxy Deployment resources small unless you specifically want to
reserve that much CPU/memory per proxy Pod. Node sizing and Pod resource sizing
are separate controls in Kubernetes.
