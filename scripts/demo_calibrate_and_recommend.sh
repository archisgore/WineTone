#!/usr/bin/env bash
# End-to-end demo of the WineTone recommender.
#
# Prereqs (run once):
#   make dev
#   make pull-tier-a
#   make db-up-bg
#   make build-all          # canonical → embeddings → clusters
#
# Then this script:
#   1. Adds 6 labels for a sample user "archis"
#   2. Fits the personal projection
#   3. Runs three recommend queries:
#       a. Without user calibration (generic)
#       b. With user calibration (personalized)
#       c. With a filter (country=France)
# Each query uses the same prompt so the contrast is visible.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

USER="archis"

echo "=== 1. Add labels (mixing first-language and personal vocabulary) ==="
# We label some specific wines. The --pick=0 takes the top fuzzy match.
.venv/bin/winetone calibrate add -u "$USER" \
    -q "Barolo" \
    -d "tar and roses, grippy as a vice, jasmine on the finish" \
    --pick 0
.venv/bin/winetone calibrate add -u "$USER" \
    -q "Pinot Noir Burgundy" \
    -d "earthy and quiet, mushroom forest floor, not loud" \
    --pick 0
.venv/bin/winetone calibrate add -u "$USER" \
    -q "Chardonnay Napa" \
    -d "buttery oak bomb, popcorn nose, way too rich for me" \
    --pick 0
.venv/bin/winetone calibrate add -u "$USER" \
    -q "Riesling Mosel" \
    -d "razor sharp acid, petrol whisper, lime peel" \
    --pick 0
.venv/bin/winetone calibrate add -u "$USER" \
    -q "Cabernet Sauvignon Napa" \
    -d "tannic monolith, cassis and graphite, decant 4 hours" \
    --pick 0
.venv/bin/winetone calibrate add -u "$USER" \
    -q "Champagne Brut" \
    -d "fine bubbles, brioche and citrus, perfect celebration wine" \
    --pick 0

echo
echo "=== 2. Show the labels archis recorded ==="
.venv/bin/winetone calibrate labels -u "$USER"

echo
echo "=== 3. Fit archis's personal projection ==="
.venv/bin/winetone calibrate fit -u "$USER"

echo
echo "=== 4. Generic vs personalized recommendations ==="
echo
echo "--- A. Generic (no user; baseline) ---"
.venv/bin/winetone recommend "earthy and grippy with jasmine notes" -k 5

echo
echo "--- B. Personalized for archis (same query, his vocabulary) ---"
.venv/bin/winetone recommend "earthy and grippy with jasmine notes" -u "$USER" -k 5

echo
echo "--- C. Personalized + filtered to France ---"
.venv/bin/winetone recommend "earthy and grippy with jasmine notes" -u "$USER" -k 5 --country France

echo
echo "=== Done. ==="
