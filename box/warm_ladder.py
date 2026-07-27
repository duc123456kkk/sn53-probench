#!/usr/bin/env python3
"""Thang warm-up JIT: mac dinh 5.6k -> 12.9k -> 17.2k -> 30k prompt, hidden-states
ON (I-33). Chay sau MOI lan serve (re)start de moi shape probe lon deu compile san.

  python3 warm_ladder.py                       # thang mac dinh
  python3 warm_ladder.py --sizes 5650,12900    # token THAT, csv
  python3 warm_ladder.py --port 8001 --timeout 1800
Env fallback: SERVE_PORT. Lan dau cham shape lon co the RAT lau (compile) —
do chinh la muc dich; timeout mac dinh 1800s de khong chet giua con JIT."""
import argparse, json, os, time, urllib.request

TOK5 = [9707, 271, 3838, 374, 279]   # pattern probe chuan — khop 2 tool bench

p = argparse.ArgumentParser()
p.add_argument("--sizes", default="5650,12900,17185,30040",
               help="cac muc prompt (token THAT), csv, tang dan; "
                    "30040 = esc30k (6008x5) khop shape 2 tool bench")
p.add_argument("--out", type=int, default=128, help="max_new_tokens moi buoc")
p.add_argument("--port", type=int, default=int(os.environ.get("SERVE_PORT", "8000")))
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--timeout", type=int, default=1800)
a = p.parse_args()

for tok in [int(x) for x in a.sizes.split(",")]:
    prompt = TOK5 * max(1, round(tok / 5))
    body = json.dumps({"input_ids": prompt,
                       "sampling_params": {"max_new_tokens": a.out,
                                           "ignore_eos": True},
                       "return_hidden_states": True}).encode()
    t0 = time.time()
    r = urllib.request.Request(f"http://{a.host}:{a.port}/generate", data=body,
                               headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(r, timeout=a.timeout).read())
    print(f"WARM {len(prompt)} tok: {time.time()-t0:.1f}s "
          f"out={len(d.get('output_ids', []))}", flush=True)
print("LADDER_DONE", flush=True)
