#!/bin/bash
# stage1_bench — bringup stack + model de BENCH (khong cai miner).
# Yeu cau: driver CUDA >= 13.0 (nvidia-smi goc phai), Ubuntu 24.04 image.
# Dung: bash box/stage1_bench.sh          (~10-20' tuy mang; model 35GB tai song song)
# Guard tuy chon: GUARD_HOST=<hostname> bash box/stage1_bench.sh
set -u
[ -n "${GUARD_HOST:-}" ] && [ "$(hostname)" != "$GUARD_HOST" ] && { echo "SAI BOX: $(hostname)"; exit 99; }
set -x
mkdir -p /root/models /root/logs /root/fleet/servepatch
pip install --break-system-packages -q "huggingface_hub[cli]" 2>&1 | tail -1
nohup python3 -c "
from huggingface_hub import snapshot_download
snapshot_download('Qwen/Qwen3.6-35B-A3B-FP8', local_dir='/root/models/Qwen3.6-35B-A3B-FP8')
print('MODEL_DONE')" > /root/model_dl.log 2>&1 &
MPID=$!
wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb -O /root/ck.deb && dpkg -i /root/ck.deb
apt-get update -qq && apt-get install -y -qq cuda-toolkit-13-0 libnuma1 2>&1 | tail -2
export CUDA_HOME=/usr/local/cuda; export PATH=/usr/local/cuda/bin:$PATH
nvcc --version | tail -1
pip install --break-system-packages -q "sglang==0.5.15.post1" "transformers==5.12.1" requests 2>&1 | tail -2
python3 -c "import torch;a=torch.randn(256,256,device='cuda');b=a@a;print('MATMUL_OK',torch.__version__,torch.version.cuda)"
python3 -c "import sgl_kernel;print('sgl_kernel OK')" || pip install --break-system-packages -q --force-reinstall --no-deps "sglang-kernel==0.4.4" --index-url https://docs.sglang.ai/whl/cu129/
cp "$(dirname "$0")/sitecustomize.py" /root/fleet/servepatch/sitecustomize.py
md5sum /root/fleet/servepatch/sitecustomize.py
wait $MPID; tail -1 /root/model_dl.log; du -sh /root/models/Qwen3.6-35B-A3B-FP8
echo STAGE1_DONE
