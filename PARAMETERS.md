# PARAMETERS — tài liệu tham số đầy đủ

## Kiến trúc 4 lớp

Ưu tiên: **CLI flag > `--profile` file.json > env `SN53_<DEST>` > default trong code**.

- **Default** = số đã hiệu chuẩn bằng incident thật trên fleet (bảng "ghim" bên dưới).
- **Profile** = đặc tả theo class box, commit vào repo (`profiles/*.json`), `git pull` là có.
- **Env** = tiện cho wrapper/supervisor: `SN53_OUT_LADDER=4096,8192` ⇔ `--out-ladder`.
- **CLI** = thí nghiệm một lần, thắng tất cả.

Quy tắc tên **máy móc 1-1**: flag `--out-ladder` ⇔ dest/JSON key `out_ladder` ⇔ env `SN53_OUT_LADDER`. Suy được từ `--help`. Ngoại lệ duy nhất: alias `--out` của probench trỏ về dest `report` — JSON/env dùng `report`/`SN53_REPORT`. Mỗi key bị override được in `[cfg] key=value (nguồn)` lúc start; giá trị sai `choices` bị chặn ở mọi lớp (không chỉ CLI). Toàn bộ config hiệu lực được nhúng vào JSON kết quả (`effective_config`) — với sn53_bench thì chỉ stage wave/all mới ghi file kết quả; recon/warm in ra stdout thôi.

Profile JSON có 3 section:
```json
{ "common":   { "price_day": 13, "miner_tax": 2.3 },
  "probench": { "scenarios": "probe,chat", "concurrency": "1,4,8" },
  "bench":    { "mi_ladder": "4,6,8", "out_ladder": "4096" } }
```
`common` áp cho cả 2 tool (key lạ với tool nào thì tool đó bỏ qua); section riêng đè `common`; key sai trong section riêng = lỗi cứng kèm danh sách hợp lệ.

## Scenario tuỳ chỉnh (sn53_probench)

Grammar: `--scenarios probe,chat,myshape=20000/2048`
- tên trần phải tồn tại sẵn (`probe`, `probe8k`, `chat`, `agentic`, `prefill`) — sai tên = lỗi cứng (không còn nuốt im lặng);
- `name=IN/OUT` thêm shape mới hoặc **đè** shape có sẵn (`probe=12900/6144`);
- shape có `IN ≥ --verdict-min-input` (mặc định 10.000) tự động được chấm PASS/FAIL trong verdict SN53;
- concurrency vẫn orthogonal ở `--concurrency` (ladder áp cho mọi scenario).

Bên sn53_bench tương tự nhưng theo TOKEN THẬT: `--shapes qual=12900,esc30k=30040` (nội bộ chia 5 vì prompt được dựng từ pattern 5-token).

## Default bị GHIM bởi incident

**Nhóm 1 — override sẽ in `[CANH BAO]` kèm lý do:**

| param | default | ghim bởi |
|---|---|---|
| `miner_tax` | 2.3 | I-34: probe thật 6,6 tok/s vs bench serve-only 14,9 (PRO6000+EPYC) — thuế parse+proof+ws qua 1 GIL. CPU khác phải đo lại bằng `secs=` trong REQ ledger |
| `deadline` | 1800 | Hằng của GATEWAY (MAX_REQUEST_S) — sửa chỉ tự lừa biên |
| `margin` | 2.0 | I-34: biên thật 1,46× là mức đã giết 9 box p6 bằng 504 |
| `ttft_gate` (probench) | 90 | I-31: gate gateway ~100s; p99 170-216s từng suýt giết 2 hotkey |
| `rows_per_gb` (bench) | 360 | I-35: mép OOM đo bằng 3 xác serve (60G chết 33k rows · 141G chết 65k · 241G sống 65k); an toàn hiệu dụng = 360×0.75 ≈ 270 rows/GB |
| `ram_safe` (bench) | 0.75 | I-35: d6a oom_kill=5 khi peak vượt ngưỡng này |

**Nhóm 2 — ghim qua advisor/mặc định, override im lặng (chỉnh theo class là hợp lệ):**

| param | default | ghim bởi |
|---|---|---|
| `mem_frac` advisor | 0.90/0.85/0.83 theo class | I-28: headroom là GB TUYỆT ĐỐI sau 34,19GB weights, không phải % — copy 0.90 sang card nhỏ đã giết serve |
| cutlass sm_90 (`fp8_backend` auto) | bật | I-32: không cutlass = DeepGEMM JIT 10-20′ chặn forward > watchdog 300s → serve tự sát |
| `drop_cache` auto | RAM<100G | I-34: page cache model 35GB đội baseline; fr90a OOM 6 lần kể cả 1 request |
| `warm_shapes` đủ thang trước dial | mini→30k | I-33: cú JIT 30k đầu tiên trên tp2 = load 108, 4 leg rơi, 504 |
| TOK5 pattern | cố định, KHÔNG có flag | CHỦ ĐÍCH: đổi pattern là lệch cache/JIT với probe thật của qualifier |

## sn53_probench.py — toàn bộ flag

| flag | default | ý nghĩa |
|---|---|---|
| `--profile` | — | file JSON 3-section (trên) |
| `--scenarios` | probe,chat,agentic,prefill | grammar `name=IN/OUT`; `probe8k` phải gọi tường minh khi cần verdict 8192 (cấm ngoại suy — I-34) |
| `--concurrency` | 1,4,8,16 | ladder; mỗi mức → `--max-concurrency` của bench_serving |
| `--prompts-per-conc` | 3 | num_prompts = conc × hệ số |
| `--budget-tokens` | 700000 | trần MỀM input-token/run — luôn bắn tối thiểu conc+1 request để lấp đủ concurrency, nên có thể vượt khi (conc+1)×IN > budget |
| `--quick` | off | chat @ c=1,4 — smoke cơ chế, KHÔNG đại diện |
| `--host` / `--port` | 127.0.0.1 / 8000 (env `SERVE_PORT` fallback) | serve đích |
| `--supervisor-conf` | /root/fleet/supervisord.conf | nơi check miner đang chạy (fail-closed) |
| `--miner-tax` | 2.3 | GHIM I-34 |
| `--deadline` / `--margin` / `--ttft-gate` | 1800 / 2.0 / 90 | GHIM (bảng trên) |
| `--verdict-min-input` | 10000 | shape IN ≥ mức này mới vào verdict |
| `--price-day` / `--pool-day-usd` | 0 / 12000 | breakeven share % = price/pool×100 |
| `--skip-hs-tax` | off | bỏ phép đo thuế hidden-states |
| `--hs-shape` / `--hs-conc` | 1024/512 / 4 | shape phép đo hs_tax |
| `--gpus` | all (env `GPU_IDX` fallback) | 'all' hoặc '0,1' — telemetry đo MỌI GPU, summary per-GPU + aggregate (power/VRAM = SUM; SM%/BW% = dải min–max avg các card → skew tp2 lộ ở đây) |
| `--dmon-interval` | 1 | giây/sample nvidia-smi |
| `--seed` / `--warmup-requests` | 42 / 1 | pass-through bench_serving (giữ cố định để so giữa box) |
| `--bench-serving-extra` | "" | chuỗi nối VERBATIM vào lệnh sglang.bench_serving — van xả cho mọi flag upstream tương lai |
| `--bench-timeout` | 3600 | SIGKILL 1 run sau N giây |
| `--gen-timeout` | 1750 | timeout HTTP /generate (giữ < deadline) |
| `--tmp-dir` | /tmp | chỗ chứa jsonl trung gian |
| `--report` (alias `--out`) | /root/probench_report | prefix file .json + .md |
| `--force` | off | chạy kể cả khi engy-miner RUNNING (I-31!) |

## sn53_bench.py — toàn bộ flag

| flag | default | ý nghĩa |
|---|---|---|
| `--profile` | — | như trên |
| `--stage` | all | recon / warm / wave / all |
| `--mi-ladder` | 8,16,24 | các mức MI thử tuần tự; CPU server nên `4,6,8` (I-35) |
| `--out-ladder` | 4096 | các mức MAX_OUTPUT; 8192 phải ĐO (cấm ngoại suy) |
| `--shapes` | — | đè/thêm shape theo token thật: `qual=12900,esc30k=30040` |
| `--wave-shape` | qual | shape prompt wave chính |
| `--esc-shape` / `--esc-out` | esc30k / 256 | wave escalation (shape từng giết fr90a) |
| `--esc-penalty` | 2 | trừ MI khi fail escalation |
| `--warm-shapes` | mini,qual,esc17k,esc30k | thang JIT; esc17k thêm 27/7 (probe 17k có thật) |
| `--warm-out` | 128 | out mỗi bậc warm |
| `--jit-flag` | 30 | warm chậm hơn N giây → gắn cờ JIT storm |
| `--miner-tax` / `--deadline` / `--margin` | 2.3 / 1800 / 2.0 | GHIM |
| `--rows-per-gb` / `--ram-safe` | 360 / 0.75 | GHIM I-35 (trần rows = RAM × rows × safe / OUT) |
| `--tp` | 0=auto | override advisor cho class GPU lạ (4×24GB, 80GB đơn…) — tự chịu I-28 |
| `--mem-frac` | 0=auto | override mem-fraction-static |
| `--fp8-backend` | auto | auto=cutlass khi sm_90; cutlass/none ép tay |
| `--drop-cache` | auto | auto=bật khi RAM < ngưỡng dưới |
| `--drop-cache-below-gb` | 100 | ngưỡng RAM (GB) cho drop-cache auto |
| `--host` / `--port` | 127.0.0.1 / 8000 (env `SERVE_PORT` fallback — hoà hợp với probench, bản cũ hardcode) | serve đích; emit_config in đúng port này |
| `--gen-timeout` / `--alive-timeout` | 1750 / 5 | HTTP timeouts |
| `--cgroup-dir` | /sys/fs/cgroup | đọc memory.max/current (container khác chuẩn thì trỏ lại) |
| `--ram-poll` | 3 | giây/lần đọc RAM peak |
| `--egress-url` | https://api.engy.ai/cdn-cgi/trace | endpoint check tunnel I-25 (dùng với `--ssh-ip`) |
| `--result-out` | /root/bench_result.json | file kết quả (đổi tên khi bench nhiều config để khỏi ghi đè) |
| `--price-day` / `--pool-day-usd` | 0 / 12000 | kinh tế |
| `--be-good` / `--be-max` | 0.30 / 0.45 | ngưỡng CHỐT / CÂN NHẮC (đo từ share thật twl40s 0.5-0.9%) |
| `--min-mi` / `--prefer-mi` | 4 / 6 | policy chọn combo đề xuất |
| `--model-dir` / `--served-name` | /root/models/Qwen3.6-35B-A3B-FP8 / Qwen3.6 | dùng trong khối emit_config |
| `--fleet-dir` | /root/fleet | đường dẫn servepatch + cap file trong emit_config |
| `--chunked-prefill` / `--context-length` / `--stream-interval` | 8192 / 262144 / 8 | serve flags trong emit_config |
| `--max-running-floor` / `--max-running-mult` | 16 / 2 | max-running-requests = max(floor, MI×mult); floor 16 tồn tại vì I-31 (slot hấp thụ probe) |
| `--ssh-ip` | — | IP bạn SSH vào — bắt buộc để check tunnel |
| `--name` / `--key` | wNEW / mk-... | điền vào khối miner config |
| `--quick` / `--quick-out` | off / 512 | test cơ chế |

## box/ scripts — env

- `box/stage1_bench.sh`: `GUARD_HOST`, `MODEL_REPO`, `MODEL_DIR`, `SGLANG_VER`, `TRANSFORMERS_VER`, `CUDA_TOOLKIT_PKG`, `UBUNTU_REPO` (ubuntu2204/2404), `SGL_KERNEL_VER`, `SGL_KERNEL_INDEX`, `FLEET_DIR`.
- `box/serve.sh` (tổng quát; serve_tp2.sh giờ chỉ là wrapper TP=2 và cũng honor các env này): `TP` (default = số GPU trong `CUDA_VISIBLE_DEVICES` nếu đã set, không thì số GPU vật lý; ≤2 mới auto, >2 GPU phải set tay), `MEM_FRAC` (0.83), `MAX_RUNNING` (16), `PORT` (8000, fallback `SERVE_PORT`), `CTX_LEN`, `CHUNK_PREFILL`, `STREAM_INTERVAL`, `KV_DTYPE`, `SCHED_POLICY`, `SERVEPATCH_DIR`, `DROP_CACHE` (1), `OMP_THREADS` (2), `EXTRA_FLAGS` (vd `--fp8-gemm-backend cutlass`), `CUDA_VISIBLE_DEVICES`, `MODEL_DIR`, `SERVED_NAME`, `PY`.
- `box/warm_ladder.py`: `--sizes` (token thật, csv; default 5650,12900,17185,30040 — bậc cuối 30040 = esc30k 6008×5 để khớp shape 2 tool, bản cũ là 30000), `--out`, `--port`, `--host`, `--timeout` (1800 — cú JIT đầu có thể rất lâu, đó là mục đích).

## Ví dụ

```bash
# Box 2×4090-24G, giá $13/ngày — trọn bộ theo profile
python3 sn53_probench.py --profile profiles/qualify-4090-pair.json --price-day 13
python3 sn53_bench.py    --profile profiles/qualify-4090-pair.json --price-day 13 \
                         --ssh-ip 1.2.3.4 --name r4090a --key mk-XXX

# Shape tuỳ chỉnh: nghi probe mới 20k/2k
python3 sn53_probench.py --scenarios probe,new20k=20000/2048 --concurrency 4,8

# Đo verdict 8192 tường minh (không bao giờ ngoại suy)
python3 sn53_probench.py --scenarios probe8k --concurrency 4

# CPU i9 đã hiệu chuẩn tax riêng 1.6
python3 sn53_bench.py --stage wave --mi-ladder 4,8 --miner-tax 1.6   # sẽ in [CANH BAO]

# Class GPU lạ (vd 4×24GB thử tp4) — advisor không biết, override tay
python3 sn53_bench.py --tp 4 --mem-frac 0.80 --mi-ladder 4,8

# Env layer cho wrapper
SN53_OUT_LADDER=4096,8192 SN53_PRICE_DAY=24 python3 sn53_bench.py
```

## Nguyên tắc profile

Profile theo **CLASS BOX (GPU + CPU)**, không chỉ GPU — `miner_tax` là thuế CPU-bound nên i9 vs EPYC khác số. Box mới không khớp class nào: bắt đầu từ `profiles/default.json`, đo `--miner-tax` riêng (so `secs=` trong REQ ledger với bench sau khi active), rồi commit profile mới vào repo.
