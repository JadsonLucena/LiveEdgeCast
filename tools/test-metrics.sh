#!/bin/bash

echo "🔍 Testing LiveEdgeCast Prometheus Metrics"
echo "=========================================="
echo ""

# Color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

print_test() {
    echo -e "${BLUE}▶ $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

# Test 1: Check if pods are running
print_test "Test 1: Checking if pods are running..."
PROXY_POD=$(kubectl get pods -n media -l app=rtmp-proxy -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
WORKER_PODS=$(kubectl get pods -n media -l app=rtmp-worker --no-headers 2>/dev/null | wc -l)

if [ -n "$PROXY_POD" ]; then
    print_success "Proxy pod found: $PROXY_POD"
else
    print_error "No proxy pod found!"
    exit 1
fi

if [ "$WORKER_PODS" -eq 0 ]; then
    print_warning "No worker pods running (this is expected for serverless)"
else
    print_success "Worker pods running: $WORKER_PODS"
fi

echo ""

# Test 2: Check nginx stub_status endpoint
print_test "Test 2: Testing nginx stub_status endpoint..."
echo "---"
if kubectl exec -n media "$PROXY_POD" -c nginx -- curl -s http://localhost:8080/nginx_status 2>/dev/null; then
    print_success "Nginx stub_status is working"
else
    print_error "Nginx stub_status failed"
fi
echo "---"
echo ""

# Test 3: Check nginx-exporter metrics
print_test "Test 3: Testing nginx-exporter metrics endpoint..."
echo "Fetching metrics from nginx-exporter (port 9113)..."
echo "---"
METRICS=$(kubectl exec -n media "$PROXY_POD" -c nginx-exporter -- curl -s http://localhost:9113/metrics 2>/dev/null)

if [ -n "$METRICS" ]; then
    print_success "Nginx-exporter is responding"
    echo ""
    echo "Key metrics found:"
    echo "$METRICS" | grep -E "nginx_connections_(active|waiting|accepted|handled)" | grep -v "^#"
    echo "$METRICS" | grep "nginx_http_requests_total" | grep -v "^#"
else
    print_error "Nginx-exporter not responding"
fi
echo "---"
echo ""

# Test 4: Check if Prometheus is running
print_test "Test 4: Checking Prometheus..."
PROM_POD=$(kubectl get pods -n monitoring -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

if [ -n "$PROM_POD" ]; then
    print_success "Prometheus pod found: $PROM_POD"
else
    print_error "Prometheus pod not found!"
    exit 1
fi

echo ""

# Test 5: Setup port-forward and query Prometheus
print_test "Test 5: Setting up Prometheus port-forward and querying..."

# Kill existing port-forwards
pkill -f "kubectl.*port-forward.*prometheus.*9090" 2>/dev/null || true
sleep 1

# Start port-forward
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090 >/dev/null 2>&1 &
PROM_PID=$!
sleep 3

echo "Port-forward PID: $PROM_PID"
echo ""

# Test Prometheus API health
print_test "Test 5.1: Prometheus API health check..."
if curl -s http://localhost:9090/-/healthy 2>/dev/null | grep -q "Prometheus"; then
    print_success "Prometheus API is healthy"
else
    print_warning "Prometheus API health check inconclusive"
fi

echo ""

# Query nginx metrics
print_test "Test 5.2: Querying nginx_connections_active..."
QUERY_ACTIVE='nginx_connections_active{namespace="media"}'
RESULT_ACTIVE=$(curl -s "http://localhost:9090/api/v1/query?query=${QUERY_ACTIVE}" 2>/dev/null)

if echo "$RESULT_ACTIVE" | grep -q '"status":"success"'; then
    print_success "Query successful"
    echo ""
    echo "Response:"
    echo "$RESULT_ACTIVE" | python3 -m json.tool 2>/dev/null || echo "$RESULT_ACTIVE"
else
    print_error "Query failed"
    echo "Response: $RESULT_ACTIVE"
fi

echo ""
echo "---"
echo ""

# Query active connections (active - waiting)
print_test "Test 5.3: Querying active connections (active - waiting)..."
QUERY_DIFF='nginx_connections_active{namespace="media"} - nginx_connections_waiting{namespace="media"}'
RESULT_DIFF=$(curl -s "http://localhost:9090/api/v1/query?query=${QUERY_DIFF}" 2>/dev/null)

if echo "$RESULT_DIFF" | grep -q '"status":"success"'; then
    print_success "Query successful"
    echo ""
    echo "Response:"
    echo "$RESULT_DIFF" | python3 -m json.tool 2>/dev/null || echo "$RESULT_DIFF"
else
    print_error "Query failed"
    echo "Response: $RESULT_DIFF"
fi

echo ""
echo "---"
echo ""

# Query network traffic
print_test "Test 5.4: Querying network receive rate..."
QUERY_NETWORK='rate(container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m])'
RESULT_NETWORK=$(curl -s "http://localhost:9090/api/v1/query?query=${QUERY_NETWORK}" 2>/dev/null)

if echo "$RESULT_NETWORK" | grep -q '"status":"success"'; then
    print_success "Query successful"
    echo ""
    echo "Response:"
    echo "$RESULT_NETWORK" | python3 -m json.tool 2>/dev/null || echo "$RESULT_NETWORK"
else
    print_warning "Query returned no data (expected if no traffic yet)"
    echo "Response: $RESULT_NETWORK"
fi

echo ""
echo "---"
echo ""

# Test 6: Check ServiceMonitor (if exists)
print_test "Test 6: Checking ServiceMonitor configuration..."
if kubectl get servicemonitor -n media rtmp-proxy-monitor 2>/dev/null; then
    print_success "ServiceMonitor exists"
    echo ""
    kubectl get servicemonitor -n media rtmp-proxy-monitor -o yaml
else
    print_warning "No ServiceMonitor found (Prometheus may use pod discovery)"
fi

echo ""
echo "=========================================="
echo -e "${GREEN}✅ Metrics Testing Complete!${NC}"
echo ""
echo -e "${BLUE}📊 Next Steps:${NC}"
echo "  1. Access Prometheus UI: http://localhost:9090"
echo "  2. Try these queries in the UI:"
echo "     • nginx_connections_active{namespace=\"media\"}"
echo "     • nginx_connections_active - nginx_connections_waiting"
echo "     • rate(container_network_receive_bytes_total{namespace=\"media\"}[1m])"
echo "     • rate(container_cpu_usage_seconds_total{namespace=\"media\"}[1m])"
echo ""
echo "  3. Check targets: http://localhost:9090/targets"
echo "     Look for: media/rtmp-proxy-* pods"
echo ""
echo "  4. To stop port-forward: kill $PROM_PID"
echo ""
echo -e "${YELLOW}💡 Tip: If no metrics appear, wait 30s for first scrape${NC}"
