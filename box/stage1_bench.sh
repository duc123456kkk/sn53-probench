#!/bin/bash
# stage1_bench — bringup stack + model de BENCH (khong cai miner).
# Yeu cau: driver CUDA >= 13.0 (goc phai nvidia-smi), Ubuntu 22.04/24.04 image.
# Dung: bash box/stage1_bench.sh          (~10-20' tuy mang; model 35GB tai song song)
# Env tuy chinh (default = gia tri dang chay tren fleet):
#   GUARD_HOST=<hostname>  chi chay khi dung box (chong nham box active)
#   MODEL_REPO=Qwen/Qwen3.6-35B-A3B-FP8   MODEL_DIR=/root/models/Qwen3.6-35B-A3B-FP8
#   SGLANG_VER=0.5.15.post1  TRANSFORMERS_VER=5.12.1   (doi = trim patch co the lech API — I-13)
#   CUDA_TOOLKIT_PKG=cuda-toolkit-13-0    UBUNTU_REPO=ubuntu2404 (image 22.04: ubuntu2204)
#   SGL_KERNEL_VER=0.4.4  SGL_KERNEL_INDEX=https://docs.sglang.ai/whl/cu129/
#   FLEET_DIR=/root/fleet
set -u
[ -n "${GUARD_HOST:-}" ] && [ "$(hostname)" != "$GUARD_HOST" ] && { echo "SAI BOX: $(hostname)"; exit 99; }
set -eo pipefail    # buoc nao fail la dung ngay — khong bao gio in STAGE1_DONE gia
MODEL_REPO="${MODEL_REPO:-Qwen/Qwen3.6-35B-A3B-FP8}"
MODEL_DIR="${MODEL_DIR:-/root/models/$(basename "$MODEL_REPO")}"
SGLANG_VER="${SGLANG_VER:-0.5.15.post1}"
TRANSFORMERS_VER="${TRANSFORMERS_VER:-5.12.1}"
CUDA_TOOLKIT_PKG="${CUDA_TOOLKIT_PKG:-cuda-toolkit-13-0}"
UBUNTU_REPO="${UBUNTU_REPO:-ubuntu2404}"
SGL_KERNEL_VER="${SGL_KERNEL_VER:-0.4.4}"
SGL_KERNEL_INDEX="${SGL_KERNEL_INDEX:-https://docs.sglang.ai/whl/cu129/}"
FLEET_DIR="${FLEET_DIR:-/root/fleet}"
set -x
mkdir -p "$(dirname "$MODEL_DIR")" /root/logs "$FLEET_DIR/servepatch"
pip install --break-system-packages -q "huggingface_hub[cli]" 2>&1 | tail -1
nohup python3 -c "
from huggingface_hub import snapshot_download
snapshot_download('$MODEL_REPO', local_dir='$MODEL_DIR')
print('MODEL_DONE')" > /root/model_dl.log 2>&1 &
MPID=$!
wget -q "https://developer.download.nvidia.com/compute/cuda/repos/$UBUNTU_REPO/x86_64/cuda-keyring_1.1-1_all.deb" -O /root/ck.deb && dpkg -i /root/ck.deb
apt-get update -qq && apt-get install -y -qq "$CUDA_TOOLKIT_PKG" libnuma1 2>&1 | tail -2
export CUDA_HOME=/usr/local/cuda; export PATH=/usr/local/cuda/bin:$PATH
nvcc --version | tail -1
pip install --break-system-packages -q "sglang==$SGLANG_VER" "transformers==$TRANSFORMERS_VER" requests 2>&1 | tail -2
python3 -c "import torch;a=torch.randn(256,256,device='cuda');b=a@a;print('MATMUL_OK',torch.__version__,torch.version.cuda)"
python3 -c "import sgl_kernel;print('sgl_kernel OK')" || pip install --break-system-packages -q --force-reinstall --no-deps "sglang-kernel==$SGL_KERNEL_VER" --index-url "$SGL_KERNEL_INDEX"
cp "$(dirname "$0")/sitecustomize.py" "$FLEET_DIR/servepatch/sitecustomize.py"
md5sum "$FLEET_DIR/servepatch/sitecustomize.py"
wait $MPID; tail -1 /root/model_dl.log; du -sh "$MODEL_DIR"
echo STAGE1_DONE
