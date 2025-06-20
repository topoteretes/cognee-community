#!/bin/bash

# Navigate to the workflows directory
cd "$(dirname "$0")"

# List of workflows that should only be triggered via test-suites.yml
WORKFLOWS=(
  "test_opensearch.yml"
)

for workflow in "${WORKFLOWS[@]}"; do
  if [ -f "$workflow" ]; then
    echo "Processing $workflow..."
    
    # Create a backup
    cp "$workflow" "${workflow}.bak"
    
    # Check if the file begins with a workflow_call trigger
    if grep -q "workflow_call:" "$workflow"; then
      echo "$workflow already has workflow_call trigger, skipping..."
      continue
    fi
    
    # Get the content after the 'on:' section
    on_line=$(grep -n "^on:" "$workflow" | cut -d ':' -f1)
    
    if [ -z "$on_line" ]; then
      echo "Warning: No 'on:' section found in $workflow, skipping..."
      continue
    fi
    
    # Create a new file with the modified content
    {
      # Copy the part before 'on:'
      head -n $((on_line-1)) "$workflow"
      
      # Add the new on: section that only includes workflow_call
      echo "on:"
      echo "  workflow_call:"
      echo "    secrets:"
      echo "      inherit: true"
      
      # Find where to continue after the original 'on:' section
      next_section=$(awk "NR > $on_line && /^[a-z]/ {print NR; exit}" "$workflow")
      
      if [ -z "$next_section" ]; then
        next_section=$(wc -l < "$workflow")
        next_section=$((next_section+1))
      fi
      
      # Copy the rest of the file starting from the next section
      tail -n +$next_section "$workflow"
    } > "${workflow}.new"
    
    # Replace the original with the new version
    mv "${workflow}.new" "$workflow"
    
    echo "Modified $workflow to only run when called from test-suites.yml"
  else
    echo "Warning: $workflow not found, skipping..."
  fi
done

echo "Finished modifying workflows!" 