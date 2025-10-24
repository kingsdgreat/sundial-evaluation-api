#!/bin/bash

# Test script for Dallas, Texas properties with Regrid integration
echo "🚀 Testing Regrid integration with Dallas, Texas properties"
echo "=========================================================="

# List of Dallas properties to test
properties=(
    "00-00064-105-000-0000"
    "00-00067-349-800-0000" 
    "00-83310-009-06a-0000"
    "00-00015-146-200-0000"
    "00-00075-586-900-0000"
    "00-00089-981-884-0100"
    "00-00078-207-400-0000"
)

# Function to make a single request
make_request() {
    local apn=$1
    local start_time=$(date +%s.%N)
    
    echo "📋 Starting request for APN: $apn"
    
    response=$(curl -s -X POST "http://localhost:8000/valuate-property" \
        -H "Content-Type: application/json" \
        -d "{\"apn\": \"$apn\", \"county\": \"Dallas\", \"state\": \"TX\"}" \
        -w "\n%{http_code}")
    
    local end_time=$(date +%s.%N)
    local duration=$(echo "$end_time - $start_time" | bc -l)
    
    # Extract HTTP status code (last line)
    local http_code=$(echo "$response" | tail -n1)
    local response_body=$(echo "$response" | head -n -1)
    
    if [ "$http_code" = "200" ]; then
        echo "✅ APN $apn completed in ${duration}s"
        # Extract key info from response
        echo "$response_body" | jq -r '.target_property + " | Lot Size: " + (.target_acreage | tostring) + " acres | Est. Value: $" + (.estimated_value_avg | tostring)'
    else
        echo "❌ APN $apn failed with HTTP $http_code"
        echo "Response: $response_body"
    fi
    echo "---"
}

# Start all requests in parallel
echo "🔄 Starting all requests simultaneously..."
echo ""

# Start background processes for all properties
pids=()
for apn in "${properties[@]}"; do
    make_request "$apn" &
    pids+=($!)
done

# Wait for all processes to complete
echo "⏳ Waiting for all requests to complete..."
wait

echo ""
echo "🎉 All requests completed!"
echo "=========================================================="
echo "📊 Summary:"
echo "- Total properties tested: ${#properties[@]}"
echo "- All requests ran in parallel"
echo "- Check logs above for individual results"
