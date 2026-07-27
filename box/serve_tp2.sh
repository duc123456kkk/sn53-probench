#!/usr/bin/env bash
# Serve tp2 cho box 2 GPU (4090-24G pair: MEM_FRAC 0.83 mac dinh; 5090-32G pair: 0.85).
# 24GB/card: thue weight ~17,1GB/card sau split tp2 -> headroom la GB tuyet doi (I-28).
# Neu OOM luc start hoac luc warm 30k: MEM_FRAC=0.80 bash box/serve_tp2.sh
# PYTHONPATH mang trim patch — PHAI nam ngoai repo miner (I-13).
set -u
PY="${PY:-python3}"
MODEL_DIR="${MODEL_DIR:-/root/models/Qwen3.6-35B-A3B-FP8}"
MEM_FRAC="${MEM_FRAC:-0.83}"
MAX_RUNNING="${MAX_RUNNING:-16}"

export CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-0,1}
export PYTHONPATH=/root/fleet/servepatch
export OMP_NUM_THREADS=2

if [ -x /usr/local/cuda/bin/nvcc ]; then
  export CUDA_HOME=/usr/local/cuda
  export PATH=/usr/local/cuda/bin:$PATH
fi

exec $PY -m sglang.launch_server \
  --model-path "$MODEL_DIR" \
  --served-model-name Qwen3.6 \
  --tp-size 2 --trust-remote-code \
  --kv-cache-dtype fp8_e4m3 \
  --mem-fraction-static "$MEM_FRAC" \
  --chunked-prefill-size 8192 \
  --max-running-requests "$MAX_RUNNING" \
  --stream-interval 8 \
  --context-length 262144 \
  --enable-return-hidden-states \
  --enable-cache-report \
  --schedule-policy lpm \
  --weight-loader-drop-cache-after-load \
  --host 127.0.0.1 --port 8000
