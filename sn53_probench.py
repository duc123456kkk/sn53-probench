#!/usr/bin/env python3
"""sn53_probench — benchmark GPU/LLM serving CHUYEN NGHIEP cho box SN53.

Hop nhat 3 lop do (chay tren box, canh serve dang song):
  1. OFFICIAL : sglang.bench_serving (chuan cong nghiep, fork tu vLLM) —
                TTFT/TPOT/ITL p50-p99, request & token throughput, goodput.
  2. TELEMETRY: nvidia-smi song song tung run, DO MOI GPU — SM%/MEM%/power/VRAM
                per-GPU + aggregate (skew giua cac card tp2 lo o day).
  3. SN53     : (a) do truc tiep THUE hidden-states (cung shape, HS on vs off);
                (b) verdict bien deadline sau thue miner cho ma tran MI x OUT.

Scenario mac dinh (shape THAT tu do dac 22-27/7):
  probe   : in 12.900 / out 4.096  — shape qualifier chinh
  probe8k : in 12.900 / out 8.192  — chi chay khi can verdict OUT=8192
  chat    : in  1.024 / out   512  — chat thuong
  agentic : in    365 / out 1.500  — profile paid THAT cua d6b (cached-heavy)
  prefill : in 30.000 / out   256  — escalation probe

Scenario TUY CHINH ngay tren CLI:  --scenarios probe,myshape=20000/2048
  (name=IN/OUT them hoac DE shape; ten khong co '=' phai ton tai san)

Cau hinh 4 lop, uu tien: CLI > --profile file.json > env SN53_<DEST> > default.
Profile JSON: {"common": {...}, "probench": {...}, "bench": {...}} — key = dest
argparse (vd "miner_tax", "out_ladder"). Xem PARAMETERS.md + profiles/.

Dung:
  python3 sn53_probench.py                              # full matrix (~30-50')
  python3 sn53_probench.py --quick                      # chat @ c=1,4 (~3')
  python3 sn53_probench.py --profile profiles/qualify-4090-pair.json --price-day 13
  python3 sn53_probench.py --scenarios probe,agentic --concurrency 4,8
AN TOAN: tu choi chay khi engy-miner dang RUNNING (bench se lam ban probe/paid
cua record — I-31). --force de bo qua khi hieu ro rui ro.
"""
import argparse, json, os, re, signal, statistics, subprocess, threading, time

SCEN_BUILTIN = {
    "probe":   (12900, 4096),
    "probe8k": (12900, 8192),
    "chat":    (1024, 512),
    "agentic": (365, 1500),
    "prefill": (30000, 256),
}

# Default bi GHIM boi incident — override duoc, nhung tool se canh bao (PARAMETERS.md).
PINNED_WARN = {
    "miner_tax": "MINER_TAX=2.3 la so DO (I-34, PRO6000+EPYC): chi doi khi da doi chieu secs= trong REQ ledger cua CHINH box nay.",
    "deadline": "1800s la hang cua GATEWAY (MAX_REQUEST_S) — sua so nay chi tu lua bien, request vuot van 504 (I-19).",
    "margin": "Bien <2.0 la vung da giet 9 box p6 bang 504 (bien that 1,46x = chet, I-34).",
    "ttft_gate": "Gate that cua gateway ~100s (I-31: p99 170-216s suyt giet 2 hotkey) — nang qua 90 la het bien 10%.",
}


# --- layered config: CLI > --profile > env SN53_* > default -------------------
# (duplicated by design trong sn53_bench.py — file phai tu chua; sua thi sua ca hai)
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


def parse_scenarios(spec, scen):
    """Grammar: 'probe,chat,myshape=20000/2048' — name=IN/OUT them/de shape."""
    names = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            name, io = item.split("=", 1)
            try:
                i, o = io.split("/")
                scen[name] = (int(i), int(o))
            except ValueError:
                raise SystemExit(f"scenario '{item}' sai cu phap — dung name=IN/OUT (vd myshape=20000/2048)")
            names.append(name)
        elif item in scen:
            names.append(item)
        else:
            raise SystemExit(f"scenario '{item}' khong ton tai; hop le: {sorted(scen)} "
                             f"hoac tu dinh nghia name=IN/OUT")
    return names


def sh(cmd, to=60):
    """Chay lenh; timeout thi SIGKILL CA process-group (khong de bench orphan)."""
    try:
        p = subprocess.Popen(cmd, shell=True, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return p.communicate(timeout=to)[0]
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            p.communicate()
            return f"ERR timeout {to}s"
    except Exception as e:
        return f"ERR {e}"


def miner_running(supervisor_conf):
    """Fail-CLOSED: chi coi la an toan khi CHUNG MINH duoc khong co miner.
    (a) supervisor tra loi ro rang: tin ket qua; (b) khong co supervisor/loi:
    kiem tra pgrep engy_miner (bat ca nohup/setsid — I-12); con nghi ngo = chan."""
    out = sh(f"supervisorctl -c {supervisor_conf} status")
    if re.search(r"engy-miner\S*\s+RUNNING", out):
        return True
    have_supervisor = bool(re.search(r"engy-", out))
    pg = sh("command -v pgrep >/dev/null 2>&1 "
            "&& { pgrep -af engy_miner | grep -v pgrep; true; } "
            "|| echo ERR-nopgrep")
    if "engy_miner" in pg:
        return True
    inconclusive = pg.startswith("ERR") or "ERR-nopgrep" in pg
    if not have_supervisor and inconclusive:
        return True                       # khong phan biet duoc -> chan (fail closed)
    return False


class Dmon(threading.Thread):
    """Telemetry MOI GPU: sm%, mem%(bandwidth util), power, VRAM — 1 sample/s.
    summary() = {'per': {gpu: {...}}, 'agg': {...}} — agg: power/VRAM = SUM,
    SM%/BW% = min–max cua avg tung card (skew tp2 lo o khoang cach min-max)."""
    def __init__(self, gpus="all", interval=1):
        super().__init__(daemon=True)
        self.rows, self.stop_flag = {}, False
        cmd = ["nvidia-smi",
               "--query-gpu=index,utilization.gpu,utilization.memory,"
               "power.draw,memory.used", "--format=csv,noheader,nounits",
               "-l", str(interval)]
        if gpus != "all":
            cmd[1:1] = ["-i", gpus]
        self.cmd = cmd

    def run(self):
        try:
            p = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, text=True)
        except Exception:
            return
        while not self.stop_flag:
            line = p.stdout.readline()
            if not line:
                break
            try:
                vals = [float(x) for x in line.strip().split(",")]
                if len(vals) == 5:        # dong cut (driver reset giua chung) -> bo
                    self.rows.setdefault(int(vals[0]), []).append(vals[1:])
            except (ValueError, IndexError):
                pass
        p.terminate()

    def summary(self):
        if not self.rows:
            return {}
        f = lambda c: {"avg": round(statistics.mean(c), 1), "max": round(max(c), 1)}
        per = {}
        for g, rows in sorted(self.rows.items()):
            cols = list(zip(*rows))
            per[str(g)] = {"sm_pct": f(cols[0]), "membw_pct": f(cols[1]),
                           "power_w": f(cols[2]), "vram_mb": f(cols[3])}
        sm = [v["sm_pct"]["avg"] for v in per.values()]
        bw = [v["membw_pct"]["avg"] for v in per.values()]
        agg = {"power_w_sum_avg": round(sum(v["power_w"]["avg"] for v in per.values()), 1),
               "vram_mb_sum_max": round(sum(v["vram_mb"]["max"] for v in per.values()), 1),
               "sm_pct_range": f"{min(sm):.0f}-{max(sm):.0f}",
               "membw_pct_range": f"{min(bw):.0f}-{max(bw):.0f}"}
        return {"per": per, "agg": agg}


def bench_serving(args, inp, out, conc, nprompts, tag):
    """Chay official sglang.bench_serving, tra dict ket qua da parse."""
    of = os.path.join(args.tmp_dir, f"probench_{tag}.jsonl")
    try:
        os.remove(of)
    except OSError:
        pass
    cmd = (f"python3 -m sglang.bench_serving --backend sglang "
           f"--host {args.host} --port {args.port} "
           f"--dataset-name random --random-input-len {inp} "
           f"--random-output-len {out} --random-range-ratio 1.0 "
           f"--num-prompts {nprompts} --max-concurrency {conc} "
           f"--warmup-requests {args.warmup_requests} --seed {args.seed} "
           f"--disable-tqdm --output-file {of} "
           f"{args.bench_serving_extra} 2>&1")
    t0 = time.time()
    dm = Dmon(args.gpus, args.dmon_interval); dm.start()
    raw = sh(cmd, to=args.bench_timeout)
    dm.stop_flag = True; dm.join(timeout=3)
    wall = time.time() - t0
    res = {}
    try:
        with open(of) as f:
            res = json.loads(f.readlines()[-1])
    except Exception:
        res = {"parse_error": raw[-600:]}
    keep = {k: res.get(k) for k in (
        "request_throughput", "input_throughput", "output_throughput",
        "mean_ttft_ms", "median_ttft_ms", "p99_ttft_ms",
        "mean_tpot_ms", "median_tpot_ms", "p95_tpot_ms", "p99_tpot_ms", "p95_ttft_ms",
        "mean_itl_ms", "p99_itl_ms", "mean_e2e_latency_ms", "p99_e2e_latency_ms",
        "completed", "total_input_tokens", "total_output_tokens") if k in res}
    keep.update({"wall_s": round(wall, 1), "gpu": dm.summary()})
    if "parse_error" in res:
        keep["parse_error"] = res["parse_error"]
    return keep


def hs_tax(args, inp, out, conc):
    """Do THUE hidden-states truc tiep: cung shape, HS off vs on (raw /generate)."""
    import urllib.request
    P5 = [9707, 271, 3838, 374, 279]

    def wave(hs):
        def one(_):
            body = json.dumps({"input_ids": P5 * (inp // 5),
                               "sampling_params": {"max_new_tokens": out,
                                                   "ignore_eos": True},
                               "return_hidden_states": hs}).encode()
            r = urllib.request.Request(f"http://{args.host}:{args.port}/generate",
                                       data=body,
                                       headers={"Content-Type": "application/json"})
            t0 = time.time()
            d = json.loads(urllib.request.urlopen(r, timeout=args.gen_timeout).read())
            return len(d.get("output_ids", [])), time.time() - t0
        from concurrent.futures import ThreadPoolExecutor
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=conc) as ex:
            rs = list(ex.map(one, range(conc)))
        wall = time.time() - t0
        return sum(r[0] for r in rs) / wall
    off = wave(False)
    on = wave(True)
    return {"tok_s_hs_off": round(off, 1), "tok_s_hs_on": round(on, 1),
            "hs_factor": round(off / on, 2) if on else None}


def verdict(args, results, scen):
    """Bien deadline THAT — CHI phat verdict cho OUT da do truc tiep (khong ngoai
    suy 4096->8192: decode 8192 cham hon phi tuyen, I-34), va PASS doi hoi ca
    TTFT p99 <= gate (I-31: TTFT moi la gate giet hotkey)."""
    v = {}
    for key, r in results.items():
        if "/" not in key or "output_throughput" not in r:
            continue
        s = key.split("/")[0]
        if s not in scen or scen[s][0] < args.verdict_min_input:
            continue                       # chi shape probe-class (prefill lon)
        out_meas = scen[s][1]
        conc = int(key.split("c")[-1])
        agg = r["output_throughput"] or 0
        per_real = agg / conc / args.miner_tax if conc else 0
        worst = out_meas / per_real if per_real else 9e9
        ttft_p99 = (r.get("p99_ttft_ms") or 0) / 1000
        margin = round(args.deadline / worst, 2) if worst else 0
        v[f"{s}:MI={conc},OUT={out_meas}"] = {
            "per_stream_real": round(per_real, 1),
            "worst_s": round(worst),
            "margin": margin,
            "ttft_p99_s": round(ttft_p99, 1),
            "PASS": margin >= args.margin and ttft_p99 <= args.ttft_gate,
            "tax_dung": args.miner_tax}
    if args.price_day:
        v["breakeven_share_pct"] = round(args.price_day / args.pool_day_usd * 100, 3)
    return v


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=os.environ.get("SN53_PROFILE"),
                   help="file JSON {common:{},probench:{},bench:{}} — key = dest")
    # --- what to measure ---
    p.add_argument("--scenarios", default="probe,chat,agentic,prefill",
                   help="ten co san va/hoac name=IN/OUT (vd probe,my=20000/2048)")
    p.add_argument("--concurrency", default="1,4,8,16",
                   help="ladder; moi muc thanh --max-concurrency cua bench_serving")
    p.add_argument("--prompts-per-conc", type=int, default=3,
                   help="num_prompts = conc x he so nay")
    p.add_argument("--budget-tokens", type=int, default=700_000,
                   help="tran input-token/run (chan prefill x conc cao keo dai)")
    p.add_argument("--quick", action=argparse.BooleanOptionalAction, default=False,
                   help="chi chat @ c=1,4 — smoke test co che, KHONG dai dien")
    # --- serve/target ---
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("SERVE_PORT", "8000")))
    p.add_argument("--supervisor-conf", default="/root/fleet/supervisord.conf")
    # --- SN53 verdict knobs (default GHIM boi incident — xem PARAMETERS.md) ---
    p.add_argument("--miner-tax", type=float, default=2.3, dest="miner_tax",
                   help="thue duong miner (I-34; hieu chuan PRO6000+EPYC)")
    p.add_argument("--deadline", type=float, default=1800.0,
                   help="MAX_REQUEST_S cua gateway")
    p.add_argument("--margin", type=float, default=2.0,
                   help="bien deadline toi thieu de PASS")
    p.add_argument("--ttft-gate", type=float, default=90.0,
                   help="tran TTFT p99 de PASS (gateway ~100s, bien 10%%)")
    p.add_argument("--verdict-min-input", type=int, default=10000,
                   help="shape co input >= muc nay moi vao verdict SN53")
    p.add_argument("--price-day", type=float, default=0,
                   help="gia thue $/ngay -> tinh breakeven share")
    p.add_argument("--pool-day-usd", type=float, default=12000,
                   help="pool thanh toan/ngay quy chieu cho breakeven")
    # --- hs-tax ---
    p.add_argument("--skip-hs-tax", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--hs-shape", default="1024/512", help="IN/OUT cho phep do hs_tax")
    p.add_argument("--hs-conc", type=int, default=4)
    # --- telemetry ---
    p.add_argument("--gpus", default=os.environ.get("GPU_IDX", "all"),
                   help="'all' hoac danh sach index '0,1' cho nvidia-smi")
    p.add_argument("--dmon-interval", type=int, default=1)
    # --- passthrough / plumbing ---
    p.add_argument("--seed", type=int, default=42, help="seed bench_serving (giu co dinh de so giua box)")
    p.add_argument("--warmup-requests", type=int, default=1)
    p.add_argument("--bench-serving-extra", default="",
                   help="chuoi noi VERBATIM vao lenh sglang.bench_serving")
    p.add_argument("--bench-timeout", type=int, default=3600, help="SIGKILL 1 run sau N giay")
    p.add_argument("--gen-timeout", type=int, default=1750,
                   help="timeout HTTP /generate (giu < deadline)")
    p.add_argument("--tmp-dir", default="/tmp")
    p.add_argument("--report", "--out", dest="report", default="/root/probench_report",
                   help="prefix file ket qua (.json + .md)")
    p.add_argument("--force", action=argparse.BooleanOptionalAction, default=False,
                   help="chay ke ca khi engy-miner RUNNING (I-31 — hieu ro moi dung)")
    args = parse_with_layers(p, "probench")
    try:
        hs_in, hs_out = (int(x) for x in str(args.hs_shape).split("/"))
    except ValueError:
        raise SystemExit(f"--hs-shape '{args.hs_shape}' sai cu phap — dung IN/OUT (vd 1024/512)")

    if miner_running(args.supervisor_conf) and not args.force:
        raise SystemExit("TU CHOI: engy-miner dang RUNNING — bench se lam ban "
                         "probe/paid cua record (I-31). Dung --force neu chap nhan.")
    scen = dict(SCEN_BUILTIN)
    names = parse_scenarios(args.scenarios, scen)
    concs = [int(c) for c in str(args.concurrency).split(",")]
    if args.quick:
        names, concs = parse_scenarios("chat", scen), [1, 4]

    hw = {"gpu": sh("nvidia-smi --query-gpu=name,memory.total,driver_version "
                    "--format=csv,noheader").strip(),
          "cpu": sh("grep -m1 'model name' /proc/cpuinfo").split(":")[-1].strip(),
          "ram_gb": round(int(sh("free -b | awk '/Mem:/{print $2}'") or 0) / 2**30)}
    print(json.dumps(hw, ensure_ascii=False), flush=True)

    results = {}
    for s in names:
        inp, out = scen[s]
        for c in concs:
            tag = f"{s}/c{c}"
            n = min(max(c * args.prompts_per_conc, c + 1),
                    max(c + 1, args.budget_tokens // max(inp, 1)))
            print(f"--- {tag}: in={inp} out={out} n={n} ...", flush=True)
            results[tag] = bench_serving(args, inp, out, c, n, tag.replace("/", "_"))
            json.dump(results, open(args.report + ".json", "w"), indent=1)
            r = results[tag]
            if "output_throughput" in r and r["output_throughput"]:
                g = r.get("gpu", {}).get("agg", {})
                print(f"    out {r['output_throughput']:.0f} tok/s | "
                      f"TTFT p99 {r.get('p99_ttft_ms', 0)/1000:.2f}s | "
                      f"TPOT p99 {r.get('p99_tpot_ms', 0):.0f}ms | "
                      f"SM {g.get('sm_pct_range', '?')}% | "
                      f"BW {g.get('membw_pct_range', '?')}% | "
                      f"{g.get('power_w_sum_avg', '?')}W", flush=True)
            else:
                print(f"    LOI: {str(r.get('parse_error'))[:200]}", flush=True)

    if not args.skip_hs_tax:
        print(f"--- do thue hidden-states ({args.hs_shape}, c={args.hs_conc}) ...", flush=True)
        try:
            results["hs_tax"] = hs_tax(args, hs_in, hs_out, args.hs_conc)
        except Exception as e:
            results["hs_tax"] = {"error": repr(e)[:200]}
        print(f"    {results['hs_tax']}", flush=True)

    results["verdict_sn53"] = verdict(args, results, scen)
    results["hw"] = hw
    results["effective_config"] = args.effective_config
    json.dump(results, open(args.report + ".json", "w"), indent=1)

    # bao cao markdown gon
    md = [f"# sn53_probench — {hw['gpu']}", ""]
    md.append("| scenario/conc | out tok/s | TTFT p99 | TPOT p99 | SM% | BW% | W(sum) |")
    md.append("|---|---|---|---|---|---|---|")
    for k, r in results.items():
        if "/" not in k or "output_throughput" not in r:
            continue
        g = r.get("gpu", {}).get("agg", {})
        md.append(f"| {k} | {r.get('output_throughput', 0):.0f} "
                  f"| {r.get('p99_ttft_ms', 0)/1000:.2f}s "
                  f"| {r.get('p99_tpot_ms', 0):.0f}ms "
                  f"| {g.get('sm_pct_range', '?')} "
                  f"| {g.get('membw_pct_range', '?')} "
                  f"| {g.get('power_w_sum_avg', '?')} |")
    md += ["", f"## Verdict SN53 (sau thue miner x{args.miner_tax})", "```",
           json.dumps(results["verdict_sn53"], indent=1), "```"]
    if "hs_tax" in results:
        md += [f"\nThue hidden-states do truc tiep: {results['hs_tax']}"]
    open(args.report + ".md", "w").write("\n".join(md))
    print(f"\nDA LUU: {args.report}.json + .md", flush=True)


if __name__ == "__main__":
    main()
