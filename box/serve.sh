#!/usr/bin/env bash
# Serve tong quat cho MOI class box — moi tham so qua env, default an toan.
#   TP=2 MEM_FRAC=0.85 bash box/serve.sh          # 5090-pair
#   TP=1 MEM_FRAC=0.90 bash box/serve.sh          # PRO6000 96G
#   EXTRA_FLAGS="--fp8-gemm-backend cutlass" ...  # Hopper sm_90 (I-32, BAT BUOC)
#   (EXTRA_FLAGS tach theo space — value KHONG duoc chua space-trong-quote/glob)
# TP mac dinh = so GPU nhin thay (1 hoac 2); >2 GPU thi PHAI set TP tay.
# MEM_FRAC mac dinh 0.83 (an toan cho 48G-don va 24G-pair; I-28 — headroom la GB
# tuyet doi). OOM luc start/warm 30k: ha MEM_FRAC=0.80.
# PYTHONPATH mang trim patch — PHAI nam ngoai repo miner (I-13).
set -u
PY="${PY:-python3}"
MODEL_DIR="${MODEL_DIR:-/root/models/Qwen3.6-35B-A3B-FP8}"
SERVED_NAME="${SERVED_NAME:-Qwen3.6}"
if [ -n "${CUDA_VISIBLE_DEVICES:-}" ]; then
  N_GPU=$(echo "$CUDA_VISIBLE_DEVICES" | awk -F, '{print NF}')   # TP theo GPU user chon
else
  N_GPU=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ')           # loc dong loi NVML/MIG
fi
[ "$N_GPU" -ge 1 ] || { echo "Khong thay GPU (driver chet hoac nvidia-smi vang)"; exit 2; }
TP="${TP:-$([ "$N_GPU" -le 2 ] && echo "$N_GPU" || echo 0)}"
case "$TP" in ''|*[!0-9]*) echo "TP='$TP' khong phai so"; exit 2;; esac
[ "$TP" -ge 1 ] || { echo "Box $N_GPU GPU: set TP tay (vd TP=2 CUDA_VISIBLE_DEVICES=0,1)"; exit 2; }
MEM_FRAC="${MEM_FRAC:-0.83}"
MAX_RUNNING="${MAX_RUNNING:-16}"
PORT="${PORT:-${SERVE_PORT:-8000}}"
CTX_LEN="${CTX_LEN:-262144}"
CHUNK_PREFILL="${CHUNK_PREFILL:-8192}"
STREAM_INTERVAL="${STREAM_INTERVAL:-8}"
KV_DTYPE="${KV_DTYPE:-fp8_e4m3}"
SCHED_POLICY="${SCHED_POLICY:-lpm}"
SERVEPATCH_DIR="${SERVEPATCH_DIR:-/root/fleet/servepatch}"
DROP_CACHE="${DROP_CACHE:-1}"        # 1 = them --weight-loader-drop-cache-after-load (I-34)
EXTRA_FLAGS="${EXTRA_FLAGS:-}"

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-$(seq -s, 0 $((TP-1)))}"
export PYTHONPATH="$SERVEPATCH_DIR"
export OMP_NUM_THREADS="${OMP_THREADS:-${OMP_NUM_THREADS:-2}}"

if [ -x /usr/local/cuda/bin/nvcc ]; then
  export CUDA_HOME=/usr/local/cuda
  export PATH=/usr/local/cuda/bin:$PATH
fi

DC_FLAG=""
[ "$DROP_CACHE" = "1" ] && DC_FLAG="--weight-loader-drop-cache-after-load"

exec $PY -m sglang.launch_server \
  --model-path "$MODEL_DIR" \
  --served-model-name "$SERVED_NAME" \
  --tp-size "$TP" --trust-remote-code \
  --kv-cache-dtype "$KV_DTYPE" \
  --mem-fraction-static "$MEM_FRAC" \
  --chunked-prefill-size "$CHUNK_PREFILL" \
  --max-running-requests "$MAX_RUNNING" \
  --stream-interval "$STREAM_INTERVAL" \
  --context-length "$CTX_LEN" \
  --enable-return-hidden-states \
  --enable-cache-report \
  --schedule-policy "$SCHED_POLICY" \
  $DC_FLAG $EXTRA_FLAGS \
  --host 127.0.0.1 --port "$PORT"
