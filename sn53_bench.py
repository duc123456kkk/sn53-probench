#!/usr/bin/env python3
"""sn53_bench — gia lap probe qualifier ~90-95% de chot MI/tham so cho MOI box.

Flow: benchmark -> tham so phu hop -> so voi gia thue -> chot.

Tai hien 4 killer THAT da do duoc tren fleet (22-27/7/2026):
  1. OOM detokenizer  (I-28/36b): rows = MI x MAX_OUT vs RAM cgroup (~1.100 rows/GB)
  2. Deadline 1800s   (I-19/504): per-stream toc do that o shape 12.9k->8192, bien >=2x
  3. JIT-lanh         (I-32/33) : lan dau cham shape lon -> compile storm -> 504 wave
  4. Wave dong thoi   (do 27/7) : qualifier lap DAY MI slot cung luc, khong tuan tu

KHONG gia lap duoc (~5-10%): verdict logic gateway, cadence probe, keepalive ws
duoi bao CPU (chi thay gian tiep qua stall/latency). Tunnel: dung --ssh-ip.

Cach dung (serve phai dang chay voi --enable-return-hidden-states + trim):
  python3 sn53_bench.py --stage all --mi-ladder 4,6,8,12 --out-ladder 4096,8192 \
                        --price-day 24 --ssh-ip <IP> --name w9 --key mk-XXX
  -> quet MOI combo (OUT x MI), in MI toi da tung muc OUT, DE XUAT combo,
     va IN NGUYEN KHOI CONFIG serve+miner dung duoc ngay (tp/mem-frac/cutlass/
     drop-cache tu suy tu phan cung).
  python3 sn53_bench.py --stage recon                  # khong tao tai
  python3 sn53_bench.py --stage warm                   # chi thang JIT
  python3 sn53_bench.py --quick                        # test co che, out=512
CANH BAO: KHONG chay stage wave tren box dang onboarding/active that (chen probe).
LUAT I-35 (27/7): (1) firehose la tai LIEN TUC, khong phai 1 wave — wave don PASS
chua du; (2) CPU server (EPYC/Xeon) don luong yeu -> MI<=4 khi qualification,
chi i9/Ryzen desktop moi giu noi 8-way sustained (jp48a 0 loi vs ia6a keepalive
chet); (3) nang MI ve sau bang cap-file SAU KHI active, khong khai truoc.
"""
import argparse, json, os, subprocess, threading, time, urllib.request

SERVE = "http://127.0.0.1:8000"
TOK5 = [9707, 271, 3838, 374, 279]          # 5 token lap = shape probe chuan
SHAPES = {"mini": 8, "qual": 2580, "esc17k": 3437, "esc30k": 6008}  # x5 token
DEADLINE = 1800.0
MARGIN = 2.0            # bien deadline toi thieu (tren so DA nhan thue)
MINER_TAX = 2.3         # I-34: duong that (parse+proof+ws/GIL) cham hon serve-only
                        # do duoc: probe that 6.6 tok/s vs bench 14.9 (PRO6000@8-way)
RAM_SAFE = 0.75         # tran RAM peak cho phep
ROWS_PER_GB = 360       # HIEU CHINH I-35 27/7: mep OOM THUC thap hon nhieu so voc
                        # 3 diem do song: 60G chet ~33k rows, 141G chet ~65k (song <=45k),
                        # 241G song 65k thoai mai -> ~270 rows/GB an toan (=360*0.75)


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception:
        return ""


def gen(n_tok5, max_out, timeout=1750):
    body = json.dumps({"input_ids": TOK5 * n_tok5,
                       "sampling_params": {"max_new_tokens": max_out,
                                           "ignore_eos": True},
                       "return_hidden_states": True}).encode()
    t0 = time.time()
    r = urllib.request.Request(SERVE + "/generate", data=body,
                               headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(r, timeout=timeout).read())
    return len(d.get("output_ids", [])), time.time() - t0


def serve_alive():
    try:
        urllib.request.urlopen(SERVE + "/get_model_info", timeout=5)
        return True
    except Exception:
        return False


class RamWatch(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.peak = 0
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            try:
                cur = int(open("/sys/fs/cgroup/memory.current").read())
                self.peak = max(self.peak, cur)
            except Exception:
                pass
            time.sleep(3)


def stage_recon(args):
    out = {}
    out["cgroup_ram_gb"] = round(int(sh("cat /sys/fs/cgroup/memory.max")
                                     .replace("max", "0") or 0) / 2**30, 1)
    if not out["cgroup_ram_gb"]:
        out["cgroup_ram_gb"] = round(int(sh("free -b | awk '/Mem:/{print $2}'") or 0) / 2**30, 1)
    out["vram"] = sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader")
    out["driver_cuda"] = sh("nvidia-smi | grep -oP 'CUDA Version: \\K[0-9.]+' | head -1")
    out["cpu"] = sh("grep -m1 'model name' /proc/cpuinfo | cut -d: -f2").strip()
    out["threads"] = int(sh("nproc") or 0)
    # --- tu van cau hinh serve theo phan cung (I-28/I-32/I-34) ---
    vram_mb = [int(x) for x in sh(
        "nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits").splitlines() if x.strip()]
    cc = sh("nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1")
    out["n_gpu"], out["compute_cap"] = len(vram_mb), cc
    per_gpu = (vram_mb[0] / 1024) if vram_mb else 0
    if per_gpu >= 90:
        out["tp"], out["mem_frac"] = 1, 0.90
    elif per_gpu >= 44:
        out["tp"], out["mem_frac"] = 1, 0.83          # I-28: 48GB card
    elif len(vram_mb) >= 2 and per_gpu >= 30:
        out["tp"], out["mem_frac"] = 2, 0.85          # 2x32GB (5090 pair)
    elif len(vram_mb) >= 2 and per_gpu >= 22:
        # 2x24GB (4090 pair): tp2 -> 17.1GB weights/card, pool ~5.6GB tong
        # = dung bang class 48GB-don (jp48a) nhung bandwidth x2; frac chat hon
        # vi headroom la GB tuyet doi, khong phai % (I-28)
        out["tp"], out["mem_frac"] = 2, 0.83
    else:
        out["tp"], out["mem_frac"] = 0, 0             # khong du VRAM -> loai
    out["flags_extra"] = []
    if cc.startswith("9.0"):
        out["flags_extra"].append("--fp8-gemm-backend cutlass")   # I-32 sm_90
    if out["cgroup_ram_gb"] < 100:
        out["flags_extra"].append("--weight-loader-drop-cache-after-load")  # I-34
    if args.ssh_ip:
        eg = sh("curl -s -m 8 https://api.engy.ai/cdn-cgi/trace | grep ip= | cut -d= -f2")
        out["egress_ip"] = eg
        out["tunnel"] = ("KHONG" if eg == args.ssh_ip else
                         f"NGHI VAN TUNNEL (ssh={args.ssh_ip} egress={eg}) -> I-25 REJECT")
    # tran RAM ly thuyet theo tung muc OUT
    ram = out["cgroup_ram_gb"]
    out["mi_ceiling_ram"] = {o: int(ram * ROWS_PER_GB * RAM_SAFE / int(o))
                             for o in args.out_ladder.split(",")}
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return out


def stage_warm(args):
    """Thang JIT: lan dau >30s o bat ky shape nao = co con I-33 tren duong nay."""
    res = {}
    for name in ("mini", "qual", "esc30k"):
        try:
            o, dt = gen(SHAPES[name], 128)
            res[name] = round(dt, 1)
            flag = "  <<< JIT STORM (I-33 class) — lan 2 se nhanh" if dt > 30 else ""
            print(f"warm {name:7s} ({SHAPES[name]*5:>6} tok): {dt:6.1f}s{flag}")
        except Exception as e:
            res[name] = f"FAIL {e}"
            print(f"warm {name}: FAIL {e}")
    return res


def wave(n, n_tok5, max_out):
    """Ban n request DONG THOI (dung cau truc wave qualifier)."""
    results, errs = [], []

    def one():
        try:
            results.append(gen(n_tok5, max_out))
        except Exception as e:
            errs.append(str(e)[:80])
    rw = RamWatch(); rw.start()
    t0 = time.time()
    ts = [threading.Thread(target=one) for _ in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    wall = time.time() - t0
    rw.stop_flag = True
    tok = sum(r[0] for r in results)
    slow = max((r[1] for r in results), default=0)
    return dict(n=n, ok=len(results), err=len(errs), errs=errs[:3],
                agg=round(tok / wall, 1) if wall else 0,
                per_stream=round(tok / wall / n, 1) if wall and n else 0,
                slowest=round(slow, 1), margin=round(DEADLINE / slow, 2) if slow else 0,
                ram_peak_gb=round(rw.peak / 2**30, 1), wall=round(wall, 1))


def stage_wave(args, recon, out_tok):
    ladder = [int(x) for x in args.mi_ladder.split(",")]
    ram_cap = recon.get("cgroup_ram_gb", 9999)
    if args.quick:
        out_tok = 512
    verdict_mi = 0
    rows = []
    for n in ladder:
        if not serve_alive():
            print(f"!! serve CHET truoc wave {n} — dung ladder"); break
        print(f"--- wave {n}x{out_tok} (12.9k prompt) ...")
        r = wave(n, SHAPES["qual"], out_tok)
        rows.append(r)
        ram_frac = r["ram_peak_gb"] / ram_cap if ram_cap else 0
        real_slow = r["slowest"] * MINER_TAX          # I-34: quy ve duong that
        r["margin_real"] = round(DEADLINE / real_slow, 2) if real_slow else 0
        ok = (r["err"] == 0 and serve_alive()
              and r["margin_real"] >= MARGIN and ram_frac <= RAM_SAFE)
        print(f"    agg={r['agg']} tok/s  per-stream={r['per_stream']}  "
              f"cham nhat={r['slowest']}s serve-only -> uoc THAT {real_slow:.0f}s "
              f"(bien that {r['margin_real']}x)  "
              f"RAM {r['ram_peak_gb']}/{ram_cap}GiB  err={r['err']}  "
              f"=> {'PASS' if ok else 'FAIL'}")
        if ok:
            verdict_mi = n
        else:
            break
        # wave escalation 30k o cung muc — dung shape lam chet fr90a
        print(f"--- wave {n}x esc30k/256 ...")
        e = wave(n, SHAPES["esc30k"], 256)
        print(f"    cham nhat={e['slowest']}s err={e['err']}")
        if e["err"] or not serve_alive():
            print("    FAIL o escalation wave"); verdict_mi = max(0, verdict_mi - 2); break
    return verdict_mi, rows


def emit_config(recon, mi, out_tok, name="wNEW", key="mk-..."):
    """In khoi config dung duoc ngay tu so do."""
    extra = " \\\n  ".join(recon.get("flags_extra", []))
    extra = ("  " + extra + " \\\n") if extra else ""
    print(f"""
----- SERVE (serve_one.sh / serve_frXX.sh) -----
python3 -m sglang.launch_server \\
  --model-path /root/models/Qwen3.6-35B-A3B-FP8 --served-model-name Qwen3.6 \\
  --tp-size {recon['tp']} --trust-remote-code --kv-cache-dtype fp8_e4m3 \\
  --mem-fraction-static {recon['mem_frac']} --chunked-prefill-size 8192 \\
  --max-running-requests {max(16, mi * 2)} --stream-interval 8 \\
  --context-length 262144 --enable-return-hidden-states --enable-cache-report \\
  --schedule-policy lpm \\
{extra}  --host 127.0.0.1 --port 8000
# + PYTHONPATH=/root/fleet/servepatch (trim, I-13) + OMP_NUM_THREADS=2

----- MINER (supervisor environment=) -----
MINER_KEY="{key}",ENGY_WORKER_NAME="{name}",MAX_INFLIGHT="{mi}",\\
MAX_OUTPUT_TOKENS="{out_tok}",ENGY_CAP_FILE="/root/fleet/capacity_{name}"
# nho: warmkeeper + oom_adj 500 + onstart.sh theo rail chuan""")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stage", default="all", choices=["all", "recon", "warm", "wave"])
    p.add_argument("--mi-ladder", default="8,16,24")  # policy 27/7: MI-first
    p.add_argument("--out-ladder", default="4096",  # policy 27/7: OUT neo 4k
                   help="quet cac muc MAX_OUTPUT (tang dan)")
    p.add_argument("--price-day", type=float, default=0)
    p.add_argument("--ssh-ip", default="")
    p.add_argument("--name", default="wNEW")
    p.add_argument("--key", default="mk-...")
    p.add_argument("--quick", action="store_true")
    args = p.parse_args()

    recon = stage_recon(args)
    if recon.get("tp") == 0:
        print("LOAI: VRAM khong du chua 34.19GB weights (tp kha thi = 0)"); return
    if args.stage == "recon":
        return
    if recon.get("tunnel", "").startswith("NGHI VAN"):
        print("DUNG: tunnel — khong ben test them, tra may (I-17/I-25)"); return
    if args.stage in ("all", "warm"):
        stage_warm(args)
    if args.stage in ("all", "wave"):
        combos = {}   # out_tok -> (mi, rows)
        for out_tok in [int(x) for x in args.out_ladder.split(",")]:
            ceiling = int(recon["cgroup_ram_gb"] * ROWS_PER_GB * RAM_SAFE / out_tok)
            print(f"\n########## OUT={out_tok} (tran RAM ly thuyet MI<={ceiling}) ##########")
            mi, rows = stage_wave(args, recon, out_tok)
            combos[out_tok] = (min(mi, ceiling), rows)
            if mi == 0:
                print(f"OUT={out_tok}: khong muc MI nao PASS — bo cac OUT cao hon")
                break
        print("\n================ KET LUAN ================")
        for out_tok, (mi, _) in combos.items():
            print(f"OUT={out_tok}: MI toi da PASS = {mi}")
        # chinh sach mac dinh (bai hoc 21 + I-34): uu tien MI cao o OUT=4096;
        # OUT cao chi khi van giu duoc MI >= 6
        best = None
        for out_tok, (mi, _) in sorted(combos.items()):
            if mi >= 4 and (best is None or mi >= 6):
                best = (out_tok, mi)
        if best:
            out_tok, mi = best
            print(f"\n>>> DE XUAT: MI={mi}, OUT={out_tok}")
            emit_config(recon, mi, out_tok, args.name, args.key)
        else:
            print(">>> KHONG co combo an toan — BO may nay")
        if args.price_day:
            be = args.price_day / 12000 * 100
            print(f"\nGia ${args.price_day}/ngay -> hoa von can share ~{be:.2f}% "
                  f"(twl40s tung dat 0.5-0.9%/ve; >0.45% la vung kho)")
            print("CHOT" if be <= 0.30 else "CAN NHAC" if be <= 0.45 else "BO")
        json.dump({"recon": recon,
                   "combos": {str(k): v[0] for k, v in combos.items()},
                   "waves": {str(k): v[1] for k, v in combos.items()}},
                  open("/root/bench_result.json", "w"), indent=1)
        print("da luu /root/bench_result.json")


if __name__ == "__main__":
    main()
