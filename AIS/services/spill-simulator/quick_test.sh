#!/bin/bash
# Quick test script for oil spill fusion simulation
# Run this after all Docker services are up

echo "🧪 Quick Test: Oil Spill Fusion Simulation"
echo "=========================================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

API_URL="http://localhost:8015"

# Test 1: Health check
echo "1. Checking API health..."
if curl -s -f "${API_URL}/health" > /dev/null; then
    echo -e "${GREEN}✅ API is healthy${NC}"
else
    echo -e "${RED}❌ API is not responding${NC}"
    exit 1
fi
echo ""

# Test 2: Start simulation
echo "2. Starting oil spill simulation..."
RESPONSE=$(curl -s -X POST "${API_URL}/api/simulate/oil-spill" \
    -H "Content-Type: application/json" \
    -d '{"scenario":"wakashio_fusion_demo","speed_multiplier":60}')

if echo "$RESPONSE" | grep -q "started"; then
    echo -e "${GREEN}✅ Simulation started${NC}"
    echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
else
    echo -e "${RED}❌ Failed to start simulation${NC}"
    echo "$RESPONSE"
    exit 1
fi
echo ""

# Test 3: Monitor progress
echo "3. Monitoring simulation (this will take ~6 minutes)..."
echo "   Press Ctrl+C to stop monitoring (simulation continues)"
echo ""

START_TIME=$(date +%s)
while true; do
    STATUS=$(curl -s "${API_URL}/api/simulate/status")
    
    if echo "$STATUS" | grep -q "completed"; then
        echo ""
        echo -e "${GREEN}✅ Simulation completed!${NC}"
        echo "$STATUS" | python3 -m json.tool 2>/dev/null || echo "$STATUS"
        break
    elif echo "$STATUS" | grep -q "error"; then
        echo ""
        echo -e "${RED}❌ Simulation error${NC}"
        echo "$STATUS" | python3 -m json.tool 2>/dev/null || echo "$STATUS"
        exit 1
    elif echo "$STATUS" | grep -q "running"; then
        PROGRESS=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('progress',0)*100)" 2>/dev/null || echo "0")
        ELAPSED=$(($(date +%s) - START_TIME))
        echo -ne "   ⏳ Progress: ${PROGRESS}% | Elapsed: ${ELAPSED}s\r"
    fi
    
    sleep 2
done
echo ""

# Test 4: Check for spills
echo "4. Checking detected spills..."
SPILLS=$(curl -s "${API_URL}/api/spills")
SPILL_COUNT=$(echo "$SPILLS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

if [ "$SPILL_COUNT" -gt 0 ]; then
    echo -e "${GREEN}✅ ${SPILL_COUNT} spill(s) detected${NC}"
    echo "$SPILLS" | python3 -m json.tool 2>/dev/null | head -50
else
    echo -e "${YELLOW}⚠️  No spills detected${NC}"
fi
echo ""

# Summary
echo "=========================================="
echo "🎉 Quick test complete!"
echo ""
echo "Next steps:"
echo "  1. Open http://localhost:3000 in your browser"
echo "  2. Check the Alert Feed for spill events"
echo "  3. View detected spills on the map"
echo "  4. Review attribution in the Incident List"
echo ""
echo "For detailed testing, run:"
echo "  cd AIS/services/spill-simulator"
echo "  python test_fusion_simulation.py"
echo "=========================================="
