#!/usr/bin/env python3
"""sn53_probench — benchmark GPU/LLM serving CHUYEN NGHIEP cho box SN53.

Hop nhat 3 lop do (chay tren box, canh serve dang song):
  1. OFFICIAL : sglang.bench_serving (chuan cong nghiep, fork tu vLLM) —
                TTFT/TPOT/ITL p50-p99, request & token throughput, goodput.
  2. TELEMETRY: nvidia-smi dmon song song tung run — SM%/MEM%/power/VRAM
                -> hieu suat that cua GPU (tok/s/W, bao hoa o dau).
  3. SN53     : (a) do truc tiep THUE hidden-states (cung shape, HS on vs off);
                (b) verdict bien deadline sau thue miner x2.3 cho ma tran MI x OUT.

Scenario mac dinh (shape THAT tu do dac 22-27/7):
  probe   : in 12.900 / out 4.096  — shape qualifier chinh
  chat    : in  1.024 / out   512  — chat thuong
  agentic : in    365 / out 1.500  — profile paid THAT cua d6b (cached-heavy)
  prefill : in 30.000 / out   256  — escalation probe

Dung:
  python3 sn53_probench.py                      # full matrix (~30-50')
  python3 sn53_probench.py --quick              # chat @ c=1,4, it prompt (~3')
  python3 sn53_probench.py --scenarios probe,agentic --concurrency 4,8
  python3 sn53_probench.py --price-day 19       # them $/1M-tok, tok/s/$
AN TOAN: tu choi chay khi engy-miner dang RUNNING (bench se lam ban probe/paid
cua record — I-31). --force de bo qua khi hieu ro rui ro.
"""
import argparse, json, os, re, signal, statistics, subprocess, threading, time

PORT = int(os.environ.get("SERVE_PORT", "8000"))
SCEN = {
    "probe":   (12900, 4096),
    "probe8k": (12900, 8192),
    "chat":    (1024, 512),
    "agentic": (365, 1500),
    "prefill": (30000, 256),
}
MINER_TAX = 2.3          # I-34: mac dinh — hieu chuan tren PRO6000+EPYC 9355.
                         # Thue nay CPU-quyet-dinh: i9 nhe hon, CPU yeu nang hon.
                         # Override bang --miner-tax khi co so do rieng cua box.
DEADLINE = 1800.0
TTFT_GATE_S = 90.0       # gate TTFT p99 (gateway ~100s, biên 10%)


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


def miner_running():
    """Fail-CLOSED: chi coi la an toan khi CHUNG MINH duoc khong co miner.
    (a) supervisor tra loi ro rang: tin ket qua; (b) khong co supervisor/loi:
    kiem tra pgrep engy_miner (bat ca nohup/setsid — I-12); con nghi ngo = chan."""
    out = sh("supervisorctl -c /root/fleet/supervisord.conf status")
    if re.search(r"engy-miner\S*\s+RUNNING", out):
        return True
    have_supervisor = bool(re.search(r"engy-", out))
    pg = sh("pgrep -af engy_miner | grep -v pgrep; true")
    if "engy_miner" in pg:
        return True
    if not have_supervisor and pg.startswith("ERR"):
        return True                       # khong phan biet duoc -> chan (fail closed)
    return False


class Dmon(threading.Thread):
    """Telemetry: sm%, mem%(bandwidth util), power, VRAM — 1s/sample."""
    def __init__(self):
        super().__init__(daemon=True)
        self.rows, self.stop_flag = [], False

    def run(self):
        p = subprocess.Popen(
            ["nvidia-smi", "-i", os.environ.get("GPU_IDX", "0"),
             "--query-gpu=utilization.gpu,utilization.memory,"
             "power.draw,memory.used", "--format=csv,noheader,nounits", "-l", "1"],
            stdout=subprocess.PIPE, text=True)
        while not self.stop_flag:
            line = p.stdout.readline()
            if not line:
                break
            try:
                self.rows.append([float(x) for x in line.strip().split(",")])
            except ValueError:
                pass
        p.terminate()

    def summary(self):
        if not self.rows:
            return {}
        cols = list(zip(*self.rows))
        f = lambda c: {"avg": round(statistics.mean(c), 1), "max": round(max(c), 1)}
        return {"sm_pct": f(cols[0]), "membw_pct": f(cols[1]),
                "power_w": f(cols[2]), "vram_mb": f(cols[3])}


def bench_serving(inp, out, conc, nprompts, tag):
    """Chay official sglang.bench_serving, tra dict ket qua da parse."""
    of = f"/tmp/probench_{tag}.jsonl"
    try:
        os.remove(of)
    except OSError:
        pass
    cmd = (f"python3 -m sglang.bench_serving --backend sglang "
           f"--host 127.0.0.1 --port {PORT} "
           f"--dataset-name random --random-input-len {inp} "
           f"--random-output-len {out} --random-range-ratio 1.0 "
           f"--num-prompts {nprompts} --max-concurrency {conc} "
           f"--warmup-requests 1 --seed 42 --disable-tqdm "
           f"--output-file {of} 2>&1")
    t0 = time.time()
    dm = Dmon(); dm.start()
    raw = sh(cmd, to=3600)
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


def hs_tax(inp, out, conc):
    """Do THUE hidden-states truc tiep: cung shape, HS off vs on (raw /generate)."""
    import urllib.request
    P5 = [9707, 271, 3838, 374, 279]

    def wave(hs):
        def one(_):
            body = json.dumps({"input_ids": P5 * (inp // 5),
                               "sampling_params": {"max_new_tokens": out,
                                                   "ignore_eos": True},
                               "return_hidden_states": hs}).encode()
            r = urllib.request.Request(f"http://127.0.0.1:{PORT}/generate",
                                       data=body,
                                       headers={"Content-Type": "application/json"})
            t0 = time.time()
            d = json.loads(urllib.request.urlopen(r, timeout=1750).read())
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


def verdict(results, price_day, tax):
    """Bien deadline THAT — CHI phat verdict cho OUT da do truc tiep (khong ngoai
    suy 4096->8192: decode 8192 cham hon phi tuyen, I-34), va PASS doi hoi ca
    TTFT p99 <= gate (I-31: TTFT moi la gate giet hotkey)."""
    v = {}
    for key, r in results.items():
        if "/" not in key or "output_throughput" not in r:
            continue
        s = key.split("/")[0]
        if s not in SCEN or SCEN[s][0] < 10000:
            continue                       # chi shape probe-class (prefill lon)
        out_meas = SCEN[s][1]
        conc = int(key.split("c")[-1])
        agg = r["output_throughput"] or 0
        per_real = agg / conc / tax if conc else 0
        worst = out_meas / per_real if per_real else 9e9
        ttft_p99 = (r.get("p99_ttft_ms") or 0) / 1000
        margin = round(DEADLINE / worst, 2) if worst else 0
        v[f"MI={conc},OUT={out_meas}"] = {
            "per_stream_real": round(per_real, 1),
            "worst_s": round(worst),
            "margin": margin,
            "ttft_p99_s": round(ttft_p99, 1),
            "PASS": margin >= 2.0 and ttft_p99 <= TTFT_GATE_S,
            "tax_dung": tax}
    if price_day:
        v["breakeven_share_pct"] = round(price_day / 12000 * 100, 3)
    return v


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenarios", default="probe,chat,agentic,prefill")
    p.add_argument("--concurrency", default="1,4,8,16")
    p.add_argument("--prompts-per-conc", type=int, default=3,
                   help="num_prompts = conc x he so nay")
    p.add_argument("--price-day", type=float, default=0)
    p.add_argument("--miner-tax", type=float, default=MINER_TAX,
                   help="thue miner-path (mac dinh 2.3 — do lai cho CPU khac)")
    p.add_argument("--skip-hs-tax", action="store_true")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--out", default="/root/probench_report")
    args = p.parse_args()

    if miner_running() and not args.force:
        raise SystemExit("TU CHOI: engy-miner dang RUNNING — bench se lam ban "
                         "probe/paid cua record (I-31). Dung --force neu chap nhan.")
    scen = [s for s in args.scenarios.split(",") if s in SCEN]
    concs = [int(c) for c in args.concurrency.split(",")]
    if args.quick:
        scen, concs = ["chat"], [1, 4]

    hw = {"gpu": sh("nvidia-smi --query-gpu=name,memory.total,driver_version "
                    "--format=csv,noheader").strip(),
          "cpu": sh("grep -m1 'model name' /proc/cpuinfo").split(":")[-1].strip(),
          "ram_gb": round(int(sh("free -b | awk '/Mem:/{print $2}'") or 0) / 2**30)}
    print(json.dumps(hw, ensure_ascii=False), flush=True)

    results = {}
    for s in scen:
        inp, out = SCEN[s]
        for c in concs:
            tag = f"{s}/c{c}"
            budget = 700_000              # tran input-token/run (FIX: prefill x conc cao)
            n = min(max(c * args.prompts_per_conc, c + 1),
                    max(c + 1, budget // max(inp, 1)))
            print(f"--- {tag}: in={inp} out={out} n={n} ...", flush=True)
            results[tag] = bench_serving(inp, out, c, n, tag.replace("/", "_"))
            json.dump(results, open(args.out + ".json", "w"), indent=1)
            r = results[tag]
            if "output_throughput" in r and r["output_throughput"]:
                g = r.get("gpu", {})
                print(f"    out {r['output_throughput']:.0f} tok/s | "
                      f"TTFT p99 {r.get('p99_ttft_ms', 0)/1000:.2f}s | "
                      f"TPOT p99 {r.get('p99_tpot_ms', 0):.0f}ms | "
                      f"SM {g.get('sm_pct', {}).get('avg', '?')}% | "
                      f"BW {g.get('membw_pct', {}).get('avg', '?')}% | "
                      f"{g.get('power_w', {}).get('avg', '?')}W", flush=True)
            else:
                print(f"    LOI: {str(r.get('parse_error'))[:200]}", flush=True)

    if not args.skip_hs_tax:
        print("--- do thue hidden-states (chat-shape, c=4) ...", flush=True)
        try:
            results["hs_tax"] = hs_tax(1024, 512, 4)
        except Exception as e:
            results["hs_tax"] = {"error": repr(e)[:200]}
        print(f"    {results['hs_tax']}", flush=True)

    results["verdict_sn53"] = verdict(results, args.price_day, args.miner_tax)
    results["hw"] = hw
    json.dump(results, open(args.out + ".json", "w"), indent=1)

    # bao cao markdown gon
    md = [f"# sn53_probench — {hw['gpu']}", ""]
    md.append("| scenario/conc | out tok/s | TTFT p99 | TPOT p99 | SM% | BW% | W |")
    md.append("|---|---|---|---|---|---|---|")
    for k, r in results.items():
        if "/" not in k or "output_throughput" not in r:
            continue
        g = r.get("gpu", {})
        md.append(f"| {k} | {r.get('output_throughput', 0):.0f} "
                  f"| {r.get('p99_ttft_ms', 0)/1000:.2f}s "
                  f"| {r.get('p99_tpot_ms', 0):.0f}ms "
                  f"| {g.get('sm_pct', {}).get('avg', '?')} "
                  f"| {g.get('membw_pct', {}).get('avg', '?')} "
                  f"| {g.get('power_w', {}).get('avg', '?')} |")
    md += ["", "## Verdict SN53 (sau thue miner x2.3)", "```",
           json.dumps(results["verdict_sn53"], indent=1), "```"]
    if "hs_tax" in results:
        md += [f"\nThue hidden-states do truc tiep: {results['hs_tax']}"]
    open(args.out + ".md", "w").write("\n".join(md))
    print(f"\nDA LUU: {args.out}.json + .md", flush=True)


if __name__ == "__main__":
    main()
