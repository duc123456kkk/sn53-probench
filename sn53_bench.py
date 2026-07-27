#!/usr/bin/env python3
"""sn53_bench — gia lap probe qualifier ~90-95% de chot MI/tham so cho MOI box.

Flow: benchmark -> tham so phu hop -> so voi gia thue -> chot.

Tai hien 4 killer THAT da do duoc tren fleet (22-27/7/2026):
  1. OOM detokenizer  (I-28/36b): rows = MI x MAX_OUT vs RAM cgroup
  2. Deadline 1800s   (I-19/504): per-stream toc do that o shape 12.9k->OUT, bien >=2x
  3. JIT-lanh         (I-32/33) : lan dau cham shape lon -> compile storm -> 504 wave
  4. Wave dong thoi   (do 27/7) : qualifier lap DAY MI slot cung luc, khong tuan tu

KHONG gia lap duoc (~5-10%): verdict logic gateway, cadence probe, keepalive ws
duoi bao CPU (chi thay gian tiep qua stall/latency). Tunnel: dung --ssh-ip.

Cau hinh 4 lop, uu tien: CLI > --profile file.json > env SN53_<DEST> > default.
Profile JSON: {"common": {...}, "probench": {...}, "bench": {...}} — key = dest
argparse (vd "mi_ladder"). Xem PARAMETERS.md + profiles/.

Cach dung (serve phai dang chay voi --enable-return-hidden-states + trim):
  python3 sn53_bench.py --stage all --mi-ladder 4,6,8 --out-ladder 4096 \
                        --price-day 24 --ssh-ip <IP> --name w9 --key mk-XXX
  -> quet MOI combo (OUT x MI), in MI toi da tung muc OUT, DE XUAT combo,
     va IN NGUYEN KHOI CONFIG serve+miner dung duoc ngay.
  python3 sn53_bench.py --stage recon                  # khong tao tai
  python3 sn53_bench.py --stage warm                   # chi thang JIT
  python3 sn53_bench.py --profile profiles/qualify-4090-pair.json
  python3 sn53_bench.py --quick                        # test co che, out=512
CANH BAO: KHONG chay stage wave tren box dang onboarding/active that (chen probe).
LUAT I-35 (27/7): (1) firehose la tai LIEN TUC, khong phai 1 wave — wave don PASS
chua du; (2) CPU server (EPYC/Xeon) don luong yeu -> MI<=4 khi qualification,
chi i9/Ryzen desktop moi giu noi 8-way sustained; (3) nang MI ve sau bang
cap-file SAU KHI active, khong khai truoc.
"""
import argparse, json, os, subprocess, threading, time, urllib.request

TOK5 = [9707, 271, 3838, 374, 279]          # 5 token lap = shape probe chuan
                                            # (co dinh CO CHU DICH — khop qualifier;
                                            #  doi la lech cache/JIT voi probe that)
SHAPES = {"mini": 8, "qual": 2580, "esc17k": 3437, "esc30k": 6008}  # x5 token

PINNED_WARN = {
    "miner_tax": "MINER_TAX=2.3 la so DO (I-34, PRO6000+EPYC): chi doi khi da doi chieu secs= ledger cua CHINH box.",
    "deadline": "1800s la hang cua GATEWAY — sua chi tu lua bien; request vuot van 504 (I-19).",
    "margin": "Bien <2.0 la vung da giet 9 box p6 bang 504 (bien that 1,46x = chet, I-34).",
    "rows_per_gb": "270 rows/GB an toan (=360x0.75) la mep OOM do bang 3 xac serve (I-35) — nang len la quay lai detokenizer exit -9.",
    "ram_safe": "Peak >0.75 x cgroup la vung oom_kill tai hien 5 lan tren d6a (I-35).",
}


# --- layered config: CLI > --profile > env SN53_* > default -------------------
# (duplicated by design trong sn53_probench.py — file phai tu chua; sua thi sua ca hai)
def parse_with_layers(parser, section):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--profile", default=os.environ.get("SN53_PROFILE"))
    pre_args, _ = pre.parse_known_args()
    acts = {a.dest: a for a in parser._actions if a.dest not in ("help",)}
    orig_defaults = {d: a.default for d, a in acts.items()}
    layers = {}                                   # dest -> (raw value, source)
    for d, a in acts.items():                     # env truoc — profile de len env
        ev = os.environ.get("SN53_" + d.upper())
        if ev is not None:
            layers[d] = (ev, f"env:SN53_{d.upper()}")
    if pre_args.profile:
        prof = json.load(open(pre_args.profile))
        for sec in ("common", section):
            for k, v in (prof.get(sec) or {}).items():
                src = f"profile:{os.path.basename(pre_args.profile)}[{sec}]"
                if k == "profile":
                    continue                      # khong ho tro profile long nhau
                if k not in acts:
                    if sec == "common":
                        continue                  # common phuc vu ca 2 tool
                    raise SystemExit(f"[cfg] key '{k}' khong hop le trong section "
                                     f"'{sec}'; hop le: {sorted(acts)}")
                layers[k] = (v, src)

    def coerce(a, v):
        if isinstance(a, argparse.BooleanOptionalAction) or a.const is True:
            return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
        if isinstance(v, list):
            v = ",".join(str(x) for x in v)
        v = a.type(v) if a.type else (v if isinstance(v, str) else str(v))
        if a.choices and v not in a.choices:      # CLI co argparse check, lop nay tu check
            raise SystemExit(f"[cfg] {a.dest}={v} khong thuoc {list(a.choices)}")
        return v
    merged = {d: coerce(acts[d], v) for d, (v, _) in layers.items()}
    parser.set_defaults(**merged)
    args = parser.parse_args()
    for d, (_, src) in sorted(layers.items()):
        fin = getattr(args, d)
        tag = src if fin == merged[d] else f"CLI (de {src})"
        print(f"[cfg] {d}={fin} ({tag})", flush=True)
    for d, msg in PINNED_WARN.items():
        if d in acts and getattr(args, d) != orig_defaults.get(d):
            print(f"[CANH BAO] {d}={getattr(args, d)} — {msg}", flush=True)
    args.effective_config = {d: getattr(args, d) for d in acts if d != "profile"}
    return args


def sh(cmd, to=20):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=to).stdout.strip()
    except Exception:
        return ""


def gen(args, n_tok5, max_out):
    body = json.dumps({"input_ids": TOK5 * n_tok5,
                       "sampling_params": {"max_new_tokens": max_out,
                                           "ignore_eos": True},
                       "return_hidden_states": True}).encode()
    t0 = time.time()
    r = urllib.request.Request(args.serve_url + "/generate", data=body,
                               headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(r, timeout=args.gen_timeout).read())
    return len(d.get("output_ids", [])), time.time() - t0


def serve_alive(args):
    try:
        urllib.request.urlopen(args.serve_url + "/get_model_info",
                               timeout=args.alive_timeout)
        return True
    except Exception:
        return False


class RamWatch(threading.Thread):
    def __init__(self, cgroup_dir, poll_s):
        super().__init__(daemon=True)
        self.peak, self.stop_flag = 0, False
        self.path = os.path.join(cgroup_dir, "memory.current")
        self.poll_s = poll_s

    def run(self):
        while not self.stop_flag:
            try:
                cur = int(open(self.path).read())
                self.peak = max(self.peak, cur)
            except Exception:
                pass
            time.sleep(self.poll_s)


def stage_recon(args):
    out = {}
    out["cgroup_ram_gb"] = round(int(sh(f"cat {args.cgroup_dir}/memory.max")
                                     .replace("max", "0") or 0) / 2**30, 1)
    if not out["cgroup_ram_gb"]:
        out["cgroup_ram_gb"] = round(int(sh("free -b | awk '/Mem:/{print $2}'") or 0) / 2**30, 1)
    out["vram"] = sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader")
    out["driver_cuda"] = sh("nvidia-smi | grep -oP 'CUDA Version: \\K[0-9.]+' | head -1")
    out["cpu"] = sh("grep -m1 'model name' /proc/cpuinfo | cut -d: -f2").strip()
    out["threads"] = int(sh("nproc") or 0)
    # --- tu van cau hinh serve theo phan cung (I-28/I-32/I-34) ---
    vram_mb = [int(x.strip()) for x in sh(
        "nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits").splitlines()
        if x.strip().isdigit()]           # loc dong loi driver (NVML in ra stdout)
    cc = sh("nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1")
    out["n_gpu"], out["compute_cap"] = len(vram_mb), cc
    per_gpu = (vram_mb[0] / 1024) if vram_mb else 0
    if args.tp:                                       # override tay cho class la
        out["tp"], out["mem_frac"] = args.tp, args.mem_frac or 0.80
        out["advisor"] = "OVERRIDE tay (--tp/--mem-frac) — tu chiu trach nhiem I-28"
    elif per_gpu >= 90:
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
    if args.mem_frac and not args.tp:
        out["mem_frac"] = args.mem_frac
    out["flags_extra"] = []
    want_cutlass = (args.fp8_backend == "cutlass"
                    or (args.fp8_backend == "auto" and cc.startswith("9.0")))
    if want_cutlass:
        out["flags_extra"].append("--fp8-gemm-backend cutlass")   # I-32 sm_90
    if (args.drop_cache == "on"
            or (args.drop_cache == "auto" and out["cgroup_ram_gb"] < args.drop_cache_below_gb)):
        out["flags_extra"].append("--weight-loader-drop-cache-after-load")  # I-34
    if args.ssh_ip:
        eg = sh(f"curl -s -m 8 {args.egress_url} | grep ip= | cut -d= -f2")
        out["egress_ip"] = eg
        out["tunnel"] = ("KHONG" if eg == args.ssh_ip else
                         f"NGHI VAN TUNNEL (ssh={args.ssh_ip} egress={eg}) -> I-25 REJECT")
    # tran RAM ly thuyet theo tung muc OUT
    ram = out["cgroup_ram_gb"]
    out["mi_ceiling_ram"] = {o: int(ram * args.rows_per_gb * args.ram_safe / int(o))
                             for o in args.out_ladder.split(",")}
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return out


def parse_shapes(spec):
    """--shapes qual=12900,esc30k=30040 — TOKEN THAT, chia 5 noi bo ra don vi tok5."""
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            name, tok = item.split("=", 1)
            SHAPES[name] = max(1, round(int(tok) / 5))
        except ValueError:
            raise SystemExit(f"--shapes '{item}' sai cu phap — dung name=TOKEN "
                             f"(vd qual=12900,esc30k=30040)")


def stage_warm(args):
    """Thang JIT: lan dau >N giay o bat ky shape nao = co con I-33 tren duong nay."""
    res = {}
    for name in [s for s in args.warm_shapes.split(",") if s]:
        if name not in SHAPES:
            raise SystemExit(f"warm shape '{name}' khong ton tai; hop le: {sorted(SHAPES)}")
        try:
            o, dt = gen(args, SHAPES[name], args.warm_out)
            res[name] = round(dt, 1)
            flag = "  <<< JIT STORM (I-33 class) — lan 2 se nhanh" if dt > args.jit_flag else ""
            print(f"warm {name:7s} ({SHAPES[name]*5:>6} tok): {dt:6.1f}s{flag}")
        except Exception as e:
            res[name] = f"FAIL {e}"
            print(f"warm {name}: FAIL {e}")
    return res


def wave(args, n, n_tok5, max_out):
    """Ban n request DONG THOI (dung cau truc wave qualifier)."""
    results, errs = [], []

    def one():
        try:
            results.append(gen(args, n_tok5, max_out))
        except Exception as e:
            errs.append(str(e)[:80])
    rw = RamWatch(args.cgroup_dir, args.ram_poll); rw.start()
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
                slowest=round(slow, 1),
                margin=round(args.deadline / slow, 2) if slow else 0,
                ram_peak_gb=round(rw.peak / 2**30, 1), wall=round(wall, 1))


def stage_wave(args, recon, out_tok):
    ladder = [int(x) for x in args.mi_ladder.split(",")]
    ram_cap = recon.get("cgroup_ram_gb", 9999)
    verdict_mi = 0
    rows = []
    for n in ladder:
        if not serve_alive(args):
            print(f"!! serve CHET truoc wave {n} — dung ladder"); break
        print(f"--- wave {n}x{out_tok} ({SHAPES[args.wave_shape]*5} prompt) ...")
        r = wave(args, n, SHAPES[args.wave_shape], out_tok)
        rows.append(r)
        ram_frac = r["ram_peak_gb"] / ram_cap if ram_cap else 0
        real_slow = r["slowest"] * args.miner_tax     # I-34: quy ve duong that
        r["margin_real"] = round(args.deadline / real_slow, 2) if real_slow else 0
        ok = (r["err"] == 0 and serve_alive(args)
              and r["margin_real"] >= args.margin and ram_frac <= args.ram_safe)
        print(f"    agg={r['agg']} tok/s  per-stream={r['per_stream']}  "
              f"cham nhat={r['slowest']}s serve-only -> uoc THAT {real_slow:.0f}s "
              f"(bien that {r['margin_real']}x)  "
              f"RAM {r['ram_peak_gb']}/{ram_cap}GiB  err={r['err']}  "
              f"=> {'PASS' if ok else 'FAIL'}")
        if ok:
            verdict_mi = n
        else:
            break
        # wave escalation o cung muc — dung shape lam chet fr90a
        print(f"--- wave {n}x {args.esc_shape}/{args.esc_out} ...")
        e = wave(args, n, SHAPES[args.esc_shape], args.esc_out)
        print(f"    cham nhat={e['slowest']}s err={e['err']}")
        if e["err"] or not serve_alive(args):
            print("    FAIL o escalation wave")
            verdict_mi = max(0, verdict_mi - args.esc_penalty); break
    return verdict_mi, rows


def emit_config(args, recon, mi, out_tok):
    """In khoi config dung duoc ngay tu so do."""
    extra = " \\\n  ".join(recon.get("flags_extra", []))
    extra = ("  " + extra + " \\\n") if extra else ""
    max_running = max(args.max_running_floor, mi * args.max_running_mult)
    print(f"""
----- SERVE (serve_one.sh / box/serve.sh) -----
python3 -m sglang.launch_server \\
  --model-path {args.model_dir} --served-model-name {args.served_name} \\
  --tp-size {recon['tp']} --trust-remote-code --kv-cache-dtype fp8_e4m3 \\
  --mem-fraction-static {recon['mem_frac']} --chunked-prefill-size {args.chunked_prefill} \\
  --max-running-requests {max_running} --stream-interval {args.stream_interval} \\
  --context-length {args.context_length} --enable-return-hidden-states \\
  --enable-cache-report --schedule-policy lpm \\
{extra}  --host 127.0.0.1 --port {args.port}
# + PYTHONPATH={args.fleet_dir}/servepatch (trim, I-13) + OMP_NUM_THREADS=2

----- MINER (supervisor environment=) -----
MINER_KEY="{args.key}",ENGY_WORKER_NAME="{args.name}",MAX_INFLIGHT="{mi}",\\
MAX_OUTPUT_TOKENS="{out_tok}",ENGY_CAP_FILE="{args.fleet_dir}/capacity_{args.name}"
# nho: warmkeeper + oom_adj 500 + onstart.sh theo rail chuan""")


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=os.environ.get("SN53_PROFILE"),
                   help="file JSON {common:{},probench:{},bench:{}} — key = dest")
    p.add_argument("--stage", default="all", choices=["all", "recon", "warm", "wave"])
    # --- ladder & shapes ---
    p.add_argument("--mi-ladder", default="8,16,24",
                   help="cac muc MI thu tuan tu (CPU server: bat dau 4 — I-35)")
    p.add_argument("--out-ladder", default="4096",
                   help="quet cac muc MAX_OUTPUT (tang dan); 8192 phai DO, cam ngoai suy")
    p.add_argument("--shapes", default="",
                   help="de/them shape theo TOKEN THAT: qual=12900,esc30k=30040")
    p.add_argument("--wave-shape", default="qual", help="shape prompt cua wave chinh")
    p.add_argument("--esc-shape", default="esc30k", help="shape wave escalation")
    p.add_argument("--esc-out", type=int, default=256)
    p.add_argument("--esc-penalty", type=int, default=2,
                   help="tru MI khi fail escalation wave")
    p.add_argument("--warm-shapes", default="mini,qual,esc17k,esc30k",
                   help="thang JIT (I-33); esc17k them 27/7 — probe 17k co that")
    p.add_argument("--warm-out", type=int, default=128)
    p.add_argument("--jit-flag", type=float, default=30.0,
                   help="warm cham hon N giay -> gan co JIT storm")
    # --- verdict knobs (default GHIM boi incident — xem PARAMETERS.md) ---
    p.add_argument("--miner-tax", type=float, default=2.3, dest="miner_tax")
    p.add_argument("--deadline", type=float, default=1800.0)
    p.add_argument("--margin", type=float, default=2.0)
    p.add_argument("--rows-per-gb", type=int, default=360,
                   help="hieu chinh I-35; an toan hieu dung = so nay x ram-safe")
    p.add_argument("--ram-safe", type=float, default=0.75)
    # --- hardware advisor override ---
    p.add_argument("--tp", type=int, default=0,
                   help="override advisor (0=auto); dung cho class GPU la")
    p.add_argument("--mem-frac", type=float, default=0.0,
                   help="override mem-fraction-static (0=auto theo class)")
    p.add_argument("--fp8-backend", default="auto", choices=["auto", "cutlass", "none"],
                   help="auto: cutlass khi sm_90 (I-32)")
    p.add_argument("--drop-cache", default="auto", choices=["auto", "on", "off"],
                   help="auto: bat khi RAM < drop-cache-below-gb (I-34)")
    p.add_argument("--drop-cache-below-gb", type=float, default=100.0)
    # --- serve/target & plumbing ---
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("SERVE_PORT", "8000")))
    p.add_argument("--gen-timeout", type=int, default=1750,
                   help="timeout HTTP /generate (giu < deadline)")
    p.add_argument("--alive-timeout", type=int, default=5)
    p.add_argument("--cgroup-dir", default="/sys/fs/cgroup")
    p.add_argument("--ram-poll", type=float, default=3.0)
    p.add_argument("--egress-url", default="https://api.engy.ai/cdn-cgi/trace")
    p.add_argument("--result-out", default="/root/bench_result.json")
    # --- economics ---
    p.add_argument("--price-day", type=float, default=0)
    p.add_argument("--pool-day-usd", type=float, default=12000)
    p.add_argument("--be-good", type=float, default=0.30,
                   help="breakeven share %% <= muc nay -> CHOT")
    p.add_argument("--be-max", type=float, default=0.45,
                   help="<= muc nay -> CAN NHAC; vuot -> BO")
    # --- combo policy & emit_config ---
    p.add_argument("--min-mi", type=int, default=4, help="MI toi thieu de xet combo")
    p.add_argument("--prefer-mi", type=int, default=6, help="uu tien combo MI >= muc nay")
    p.add_argument("--model-dir", default="/root/models/Qwen3.6-35B-A3B-FP8")
    p.add_argument("--served-name", default="Qwen3.6")
    p.add_argument("--fleet-dir", default="/root/fleet")
    p.add_argument("--chunked-prefill", type=int, default=8192)
    p.add_argument("--context-length", type=int, default=262144)
    p.add_argument("--max-running-floor", type=int, default=16)
    p.add_argument("--max-running-mult", type=int, default=2)
    p.add_argument("--stream-interval", type=int, default=8)
    # --- identity & misc ---
    p.add_argument("--ssh-ip", default="", help="IP ban SSH vao — check tunnel I-25")
    p.add_argument("--name", default="wNEW")
    p.add_argument("--key", default="mk-...")
    p.add_argument("--quick", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--quick-out", type=int, default=512)
    args = parse_with_layers(p, "bench")
    args.serve_url = f"http://{args.host}:{args.port}"
    parse_shapes(args.shapes)
    for nm in [args.wave_shape, args.esc_shape] + [s for s in args.warm_shapes.split(",") if s]:
        if nm not in SHAPES:
            raise SystemExit(f"shape '{nm}' khong ton tai; hop le: {sorted(SHAPES)} "
                             f"(them bang --shapes name=TOKEN)")

    recon = stage_recon(args)
    if recon.get("tp") == 0:
        print("LOAI: VRAM khong du chua 34.19GB weights (tp kha thi = 0) — "
              "class la thi thu --tp/--mem-frac override"); return
    if args.stage == "recon":
        return
    if recon.get("tunnel", "").startswith("NGHI VAN"):
        print("DUNG: tunnel — khong ben test them, tra may (I-17/I-25)"); return
    if args.stage in ("all", "warm"):
        stage_warm(args)
    if args.stage in ("all", "wave"):
        combos = {}   # out_tok -> (mi, rows)
        out_list = [int(x) for x in args.out_ladder.split(",")]
        if args.quick:
            out_list = [args.quick_out]   # do that o quick_out — khong dan nhan ladder
        for out_tok in out_list:
            ceiling = int(recon["cgroup_ram_gb"] * args.rows_per_gb * args.ram_safe / out_tok)
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
        # OUT cao chi khi van giu duoc MI >= prefer-mi
        if args.quick:
            print(f"\n>>> QUICK/SMOKE (out={args.quick_out}) — chi test co che, "
                  f"KHONG dung so nay de chot config")
        else:
            best = None
            for out_tok, (mi, _) in sorted(combos.items()):
                if mi >= args.min_mi and (best is None or mi >= args.prefer_mi):
                    best = (out_tok, mi)
            if best:
                out_tok, mi = best
                print(f"\n>>> DE XUAT: MI={mi}, OUT={out_tok}")
                emit_config(args, recon, mi, out_tok)
            else:
                print(">>> KHONG co combo an toan — BO may nay")
        if args.price_day:
            be = args.price_day / args.pool_day_usd * 100
            print(f"\nGia ${args.price_day}/ngay -> hoa von can share ~{be:.2f}% "
                  f"(twl40s tung dat 0.5-0.9%/ve; >{args.be_max}% la vung kho)")
            print("CHOT" if be <= args.be_good else
                  "CAN NHAC" if be <= args.be_max else "BO")
        json.dump({"recon": recon,
                   "combos": {str(k): v[0] for k, v in combos.items()},
                   "waves": {str(k): v[1] for k, v in combos.items()},
                   "effective_config": args.effective_config},
                  open(args.result_out, "w"), indent=1)
        print(f"da luu {args.result_out}")


if __name__ == "__main__":
    main()
