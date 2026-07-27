#!/usr/bin/env python3
"""Thang warm-up JIT: 5.6k -> 12.9k -> 30k prompt, hidden-states ON (I-33).
Chay sau moi lan serve (re)start de moi shape probe lon deu da compile san."""
import json, os, time, urllib.request

SIZES = [1130, 2580, 6000]   # x5 token = ~5.6k / 12.9k / 30k
for n in SIZES:
    prompt = [9707, 271, 3838, 374, 279] * n
    body = json.dumps({"input_ids": prompt,
                       "sampling_params": {"max_new_tokens": 128, "ignore_eos": True},
                       "return_hidden_states": True}).encode()
    t0 = time.time()
    r = urllib.request.Request(f"http://127.0.0.1:{os.environ.get('SERVE_PORT','8000')}/generate", data=body,
                               headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(r, timeout=900).read())
    print(f"WARM {n*5} tok: {time.time()-t0:.1f}s out={len(d.get('output_ids', []))}",
          flush=True)
print("LADDER_DONE", flush=True)
