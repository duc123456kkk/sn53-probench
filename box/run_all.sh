#!/bin/bash
# run_all — TRON BO pipeline bench trong 1 lenh, idempotent, gate tung buoc:
#   pull -> screening -> stage1 -> serve -> trim -> warm -> probench -> bench wave
#
#   PRICE_DAY=13 SSH_IP=1.2.3.4 NAME=r4090a GUARD_HOST=$(hostname) \
#     PROFILE=profiles/qualify-4090-pair.json bash box/run_all.sh
#
# Env: GUARD_HOST (khuyen dung — chong nham box), PROFILE (file profiles/*.json),
#      PRICE_DAY, SSH_IP (check tunnel I-25 — bo trong = bo check), NAME, KEY,
#      SERVE_WAIT_S (900), SKIP_PROBENCH=1 / SKIP_WAVE=1 de cat bot.
# Chay lai an toan: stage1 tu bo qua khi stack+model da co; serve dang song thi
# DUNG NGUYEN (khong bao gio restart serve dang chay — bai hoc 6).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
REPO=$PWD
LOG=/root/logs; mkdir -p "$LOG"
die() { echo; echo "!!!! FAIL o buoc: $*"; exit 1; }
step() { echo; echo "==== [$(date -u +%H:%M:%S)Z] $*"; }

[ -n "${GUARD_HOST:-}" ] && [ "$(hostname)" != "$GUARD_HOST" ] && die "SAI BOX: $(hostname) != $GUARD_HOST"
PROFILE="${PROFILE:-}"; PRICE_DAY="${PRICE_DAY:-}"; SSH_IP="${SSH_IP:-}"
NAME="${NAME:-wNEW}"; KEY="${KEY:-mk-...}"
PORT="${SERVE_PORT:-8000}"
PARGS=(); [ -n "$PROFILE" ] && PARGS+=(--profile "$PROFILE")
[ -n "$PRICE_DAY" ] && PARGS+=(--price-day "$PRICE_DAY")

step "0/7 git pull"
git pull --ff-only 2>&1 | tail -1 || echo "WARN: pull fail — chay ban local hien co"

step "1/7 screening (I-25)"
EGRESS=$(curl -s -m 10 https://api.engy.ai/cdn-cgi/trace | grep '^ip=' | cut -d= -f2)
echo "egress=$EGRESS colo=$(curl -s -m 10 https://api.engy.ai/cdn-cgi/trace | grep '^colo=' | cut -d= -f2)"
if [ -n "$SSH_IP" ] && [ "$EGRESS" != "$SSH_IP" ]; then
  die "TUNNEL: ssh=$SSH_IP != egress=$EGRESS — tra box (I-25/I-17)"
fi
[ -z "$SSH_IP" ] && echo "WARN: khong co SSH_IP — bo check tunnel (tu doi chieu $EGRESS voi IP anh SSH vao)"

step "2/7 stage1 (stack + model — tu bo qua neu da co)"
if python3 -c "import sglang" 2>/dev/null && [ -f /root/models/Qwen3.6-35B-A3B-FP8/config.json ] \
   && [ -f /root/fleet/servepatch/sitecustomize.py ]; then
  echo "stack + model + trim patch da co — skip"
else
  GUARD_HOST="${GUARD_HOST:-}" bash box/stage1_bench.sh 2>&1 | tee "$LOG/stage1.log" | tail -20
  grep -q STAGE1_DONE "$LOG/stage1.log" || die "stage1 (xem $LOG/stage1.log)"
fi

step "3/7 serve (khong restart neu dang song)"
alive() { curl -s -m 5 "http://127.0.0.1:$PORT/get_model_info" >/dev/null 2>&1; }
if alive; then
  echo "serve dang song tren :$PORT — GIU NGUYEN"
else
  nohup bash box/serve.sh > "$LOG/serve.log" 2>&1 &
  SPID=$!
  echo "serve pid $SPID — cho toi da ${SERVE_WAIT_S:-900}s (load 35GB weights)..."
  for i in $(seq 1 $(( ${SERVE_WAIT_S:-900} / 5 ))); do
    alive && break
    kill -0 "$SPID" 2>/dev/null || die "serve chet luc start (tail $LOG/serve.log): $(tail -3 "$LOG/serve.log" | head -c 300)"
    sleep 5
  done
  alive || die "serve khong len sau ${SERVE_WAIT_S:-900}s (xem $LOG/serve.log)"
  echo "serve UP"
fi

step "4/7 trim armed (I-13)"
if [ -f "$LOG/serve.log" ]; then
  grep -q "trim armed" "$LOG/serve.log" && echo "trim armed OK" \
    || die "serve chay KHONG co trim patch — dung lai (I-13); kiem tra $LOG/serve.log"
else
  echo "WARN: serve co san tu truoc, khong co log de xac minh trim — tu kiem tra"
fi

step "5/7 warm ladder (I-33)"
python3 box/warm_ladder.py 2>&1 | tee "$LOG/warm.log"
grep -q LADDER_DONE "$LOG/warm.log" || die "warm ladder (JIT storm chua qua? xem $LOG/warm.log)"

if [ "${SKIP_PROBENCH:-0}" != "1" ]; then
  step "6/7 sn53_probench (matrix ~25-35')"
  python3 sn53_probench.py "${PARGS[@]}" 2>&1 | tee "$LOG/probench.log" || die "probench (xem $LOG/probench.log)"
else
  step "6/7 probench SKIPPED"
fi

if [ "${SKIP_WAVE:-0}" != "1" ]; then
  step "7/7 sn53_bench wave (~10-15')"
  WARGS=(--stage wave --name "$NAME" --key "$KEY")
  [ -n "$SSH_IP" ] && WARGS+=(--ssh-ip "$SSH_IP")
  python3 sn53_bench.py "${PARGS[@]}" "${WARGS[@]}" 2>&1 | tee "$LOG/wave.log" || die "bench wave (xem $LOG/wave.log)"
else
  step "7/7 wave SKIPPED"
fi

echo
echo "==== XONG. Ket qua:"
echo "  /root/probench_report.md + .json   (matrix + verdict + hs-tax)"
echo "  /root/bench_result.json            (wave + combo + config de xuat)"
echo "  logs: $LOG/{stage1,serve,warm,probench,wave}.log"
