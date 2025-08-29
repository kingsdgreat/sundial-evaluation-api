#!/bin/bash

# Script to copy screenshots from Docker containers to host

echo "Copying screenshots from Docker containers..."

# Create screenshots directory on host
mkdir -p ./screenshots

# Find running app containers
CONTAINERS=$(docker ps --filter "name=1743534086245-realstate-scrape-app" --format "{{.Names}}")

if [ -z "$CONTAINERS" ]; then
    echo "No running app containers found"
    exit 1
fi

# Copy screenshots from each container
for container in $CONTAINERS; do
    echo "Copying from container: $container"
    
    # Copy all PNG files from the container
    docker cp "$container:/app/" ./temp_container_files/ 2>/dev/null
    
    # Move any PNG files to screenshots directory
    if [ -d "./temp_container_files" ]; then
        find ./temp_container_files -name "*.png" -exec cp {} ./screenshots/ \; 2>/dev/null
        rm -rf ./temp_container_files
    fi
    
    # Also try copying directly
    docker cp "$container:/app/." ./screenshots/ 2>/dev/null
done

echo "Screenshots copied to ./screenshots/"
ls -la ./screenshots/*.png 2>/dev/null || echo "No screenshot files found" 