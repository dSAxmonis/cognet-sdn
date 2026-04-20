#!/bin/bash
# CogNet-SDN Bursty Traffic Generator
# This simulates real-world chaotic web and video traffic

echo "Starting Realistic Traffic Generation on Host..."

while true; do
    # Generate a random bandwidth between 1M and 50M
    BW=$(( (RANDOM % 50) + 1 ))M
    
    # Generate a random duration for the burst (1 to 5 seconds)
    TIME=$(( (RANDOM % 5) + 1 ))
    
    # Generate a random sleep interval (simulating think-time)
    SLEEP=$(( (RANDOM % 3) + 1 ))

    echo "[*] Sending micro-burst: ${BW}bps for ${TIME}s..."
    
    # Run iperf (Replace 10.0.0.4 with your target host IP)
    iperf -c 10.0.0.4 -u -b $BW -t $TIME -i 1 > /dev/null 2>&1
    
    echo "[-] Idling for ${SLEEP}s..."
    sleep $SLEEP
done
