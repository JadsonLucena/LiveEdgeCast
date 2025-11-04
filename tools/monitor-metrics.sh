#!/bin/bash

echo "📊 LiveEdgeCast - Real-time Metrics Monitor"
echo "=========================================="
echo ""
echo "Press Ctrl+C to stop monitoring"
echo ""

# Color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Check if port-forward is already running
if ! curl -s http://localhost:9090/-/healthy >/dev/null 2>&1; then
    echo "Starting Prometheus port-forward..."
    pkill -f "kubectl.*port-forward.*prometheus.*9090" 2>/dev/null || true
    sleep 1
    kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090 >/dev/null 2>&1 &
    PROM_PID=$!
    echo "Port-forward started (PID: $PROM_PID)"
    sleep 3
else
    echo "Prometheus port-forward already running"
fi

echo ""

# Function to query Prometheus
query_prom() {
    local query="$1"
    curl -s "http://localhost:9090/api/v1/query?query=${query}" 2>/dev/null | \
        python3 -c "import sys, json; data=json.load(sys.stdin); print(data['data']['result'][0]['value'][1] if data['data']['result'] else '0')" 2>/dev/null || echo "0"
}

# Monitor loop
while true; do
    clear
    echo -e "${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║       LiveEdgeCast - Real-time Metrics Dashboard              ║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    echo -e "${BLUE}⏰ $TIMESTAMP${NC}"
    echo ""
    
    # Get proxy pod count
    PROXY_PODS=$(kubectl get pods -n media -l app=rtmp-proxy --no-headers 2>/dev/null | wc -l)
    WORKER_PODS=$(kubectl get pods -n media -l app=rtmp-worker --no-headers 2>/dev/null | wc -l)
    
    echo -e "${GREEN}📦 Pod Status:${NC}"
    echo "  Proxy pods:  $PROXY_PODS"
    echo "  Worker pods: $WORKER_PODS"
    echo ""
    
    # Query metrics
    echo -e "${GREEN}🔌 TCP Connections (nginx metrics):${NC}"
    
    CONN_ACTIVE=$(query_prom 'sum(nginx_connections_active{namespace="media"})')
    CONN_WAITING=$(query_prom 'sum(nginx_connections_waiting{namespace="media"})')
    CONN_PROCESSING=$(echo "$CONN_ACTIVE - $CONN_WAITING" | bc 2>/dev/null || echo "0")
    CONN_ACCEPTED=$(query_prom 'sum(nginx_connections_accepted{namespace="media"})')
    
    echo "  Active connections:      $CONN_ACTIVE"
    echo "  Waiting (keepalive):     $CONN_WAITING"
    echo -e "  ${YELLOW}Processing (active):     $CONN_PROCESSING${NC}"
    echo "  Total accepted:          $CONN_ACCEPTED"
    echo ""
    
    # Network metrics
    echo -e "${GREEN}🌐 Network Traffic:${NC}"
    
    RX_RATE=$(query_prom 'sum(rate(container_network_receive_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m]))')
    TX_RATE=$(query_prom 'sum(rate(container_network_transmit_bytes_total{namespace="media",pod=~"rtmp-proxy-.*"}[1m]))')
    
    # Convert to Mbps
    RX_MBPS=$(echo "scale=2; $RX_RATE / 1000000 * 8" | bc 2>/dev/null || echo "0")
    TX_MBPS=$(echo "scale=2; $TX_RATE / 1000000 * 8" | bc 2>/dev/null || echo "0")
    
    echo "  Inbound (receive):       $RX_MBPS Mbps"
    echo "  Outbound (transmit):     $TX_MBPS Mbps"
    echo ""
    
    # CPU metrics
    echo -e "${GREEN}💻 CPU Usage:${NC}"
    
    CPU_USAGE=$(query_prom 'sum(rate(container_cpu_usage_seconds_total{namespace="media",pod=~"rtmp-proxy-.*",container="nginx"}[1m]))')
    CPU_PERCENT=$(echo "scale=2; $CPU_USAGE * 100" | bc 2>/dev/null || echo "0")
    
    echo "  Proxy CPU usage:         $CPU_PERCENT %"
    echo ""
    
    # Memory metrics
    echo -e "${GREEN}💾 Memory Usage:${NC}"
    
    MEM_USAGE=$(query_prom 'sum(container_memory_usage_bytes{namespace="media",pod=~"rtmp-proxy-.*",container="nginx"})')
    MEM_MB=$(echo "scale=2; $MEM_USAGE / 1024 / 1024" | bc 2>/dev/null || echo "0")
    
    echo "  Proxy memory usage:      $MEM_MB MB"
    echo ""
    
    # KEDA Scaling Status
    echo -e "${GREEN}⚖️  KEDA Scaling Status:${NC}"
    
    # Check if ScaledObjects exist
    PROXY_SCALER=$(kubectl get scaledobject rtmp-proxy-scaler -n media -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "Unknown")
    WORKER_SCALER=$(kubectl get scaledobject rtmp-worker-scaler -n media -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "Unknown")
    
    echo "  Proxy scaler:            $PROXY_SCALER"
    echo "  Worker scaler:           $WORKER_SCALER"
    echo ""
    
    # Scaling triggers
    echo -e "${GREEN}🎯 Current Scaling Metrics:${NC}"
    
    # Proxy scaling threshold (800 Mbps = 0.8 Gbps)
    INBOUND_GBPS=$(echo "scale=3; $RX_RATE / 1000000000" | bc 2>/dev/null || echo "0")
    THRESHOLD_PERCENT=$(echo "scale=1; $INBOUND_GBPS / 0.8 * 100" | bc 2>/dev/null || echo "0")
    
    echo "  Inbound traffic:         $INBOUND_GBPS Gbps (threshold: 0.8 Gbps)"
    
    if (( $(echo "$THRESHOLD_PERCENT > 80" | bc -l 2>/dev/null) )); then
        echo -e "  ${RED}⚠️  Above threshold: $THRESHOLD_PERCENT%${NC}"
    else
        echo -e "  ${GREEN}✓ Below threshold: $THRESHOLD_PERCENT%${NC}"
    fi
    
    echo ""
    
    # Worker scaling (based on active connections)
    if (( $(echo "$CONN_PROCESSING > 0" | bc -l 2>/dev/null) )); then
        echo -e "  ${YELLOW}⚠️  Active connections detected: $CONN_PROCESSING${NC}"
        echo "  Workers should scale up if > 1 connection"
    else
        echo -e "  ${GREEN}✓ No active connections (workers at 0)${NC}"
    fi
    
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}Refreshing in 5 seconds... (Ctrl+C to stop)${NC}"
    
    sleep 5
done
