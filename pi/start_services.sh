#!/bin/bash
# Start all three Pi services in the background and tail their logs.
# Usage: ./pi/start_services.sh
# Stop: Ctrl-C (traps into cleanup that kills all three).

set -euo pipefail

cd "$(dirname "$0")/.."

mkdir -p pi/logs

REC_PID=""
CAM_PID=""
AUD_PID=""

cleanup() {
  echo ""
  echo "Stopping services..."
  [ -n "$REC_PID" ] && kill "$REC_PID" 2>/dev/null || true
  [ -n "$CAM_PID" ] && kill "$CAM_PID" 2>/dev/null || true
  [ -n "$AUD_PID" ] && kill "$AUD_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  echo "All services stopped."
  exit 0
}
trap cleanup INT TERM

# Start receiver
python hardware/receiver.py > pi/logs/receiver.stdout.log 2>&1 &
REC_PID=$!
echo "receiver.py started (PID $REC_PID) on :5001"

# Start camera service
python -m pi.camera_service > pi/logs/camera_service.stdout.log 2>&1 &
CAM_PID=$!
echo "camera_service started (PID $CAM_PID) on :5002"

# Start audio service
python -m pi.audio_service > pi/logs/audio_service.stdout.log 2>&1 &
AUD_PID=$!
echo "audio_service started (PID $AUD_PID) on :5003"

# Wait 2s then probe each.
sleep 2
echo ""
echo "Probing health endpoints..."
curl -sf http://localhost:5001/health > /dev/null && echo " receiver OK" || echo " receiver FAIL"
curl -sf http://localhost:5002/health > /dev/null && echo " camera OK" || echo " camera FAIL"
curl -sf http://localhost:5003/health > /dev/null && echo " audio OK" || echo " audio FAIL"

# Print Pi IP for laptop config.
echo ""
echo "Pi IP addresses (set MONDAY_*_URL on laptop to one of these):"
hostname -I

echo ""
echo "Services running. Ctrl-C to stop all three."
wait
