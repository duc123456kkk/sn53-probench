# sn53-probench

Professional single-file benchmark suite for LLM serving on GPU boxes
(built for [sglang](https://github.com/sgl-project/sglang) backends; born from
operating an SN53/engy mining fleet, useful for any sglang deployment).

Đo hiệu năng serving LLM chuyên nghiệp trong MỘT file, không dependency ngoài
stdlib — xương sống là `sglang.bench_serving` chính chủ (bộ metric chuẩn công
nghiệp fork từ vLLM), cộng telemetry GPU song song và tầng đo riêng cho
workload verifiable-inference (hidden-states).

## Tools

| file | vai trò |
|---|---|
| `sn53_probench.py` | Benchmark chính: ma trận shape × concurrency qua `sglang.bench_serving` + telemetry `nvidia-smi` từng run + đo thuế hidden-states + verdict deadline |
| `sn53_bench.py` | Bộ đo nhanh kiểu wave (mô phỏng probe qualifier ~90%): recon phần cứng, thang JIT warm-up, wave đồng thời, tư vấn config tp/mem-frac |

## Đo được gì

- **TTFT / TPOT / ITL / E2E** — mean, median, p95, p99 (từ bench_serving official)
- **Throughput**: request/s, input tok/s, output tok/s
- **GPU telemetry per-run**: SM util %, memory-bandwidth util %, power (W), VRAM — trả lời "nghẽn ở đâu" bằng số
- **Thuế hidden-states**: cùng shape chạy `return_hidden_states` off/on → hệ số chậm (chỉ có ý nghĩa với workload verifiable-inference)
- **Verdict SN53**: biên deadline 1800s cho từng combo (concurrency × max-output) sau thuế miner-path, kèm gate TTFT p99 ≤ 90s

## Yêu cầu

- GPU có FP8 (Ada / Hopper / Blackwell — sm_89+), driver CUDA ≥ 12.8
- Python ≥ 3.10, `nvidia-smi` trong PATH
- **sglang ≥ 0.5.15 đã cài, và một serve đang chạy** (tool bắn vào serve, không tự load model)
- Tầng hs-tax cần serve bật `--enable-return-hidden-states`; không có thì chạy `--skip-hs-tax`

## Cài & test trên box mới — 5 bước

```bash
# 1. Lấy tool (single file, không pip install gì thêm)
curl -LO https://raw.githubusercontent.com/<user>/sn53-probench/main/sn53_probench.py
#    (hoặc: scp sn53_probench.py root@box:/root/)

# 2. Đảm bảo serve đang chạy — ví dụ tối thiểu:
python3 -m sglang.launch_server --model-path <MODEL_DIR> \
  --host 127.0.0.1 --port 8000 --mem-fraction-static 0.9 \
  --enable-return-hidden-states &
#    Chờ tới khi: curl -s localhost:8000/get_model_info trả 200

# 3. SMOKE TEST tool ~3 phút (chat-shape, concurrency 1 và 4, ít request)
python3 sn53_probench.py --quick
#    PASS khi: in ra bảng "out tok/s | TTFT p99 | ..." và tạo probench_report.json/.md

# 4. Full matrix (~30-50 phút tuỳ GPU)
python3 sn53_probench.py --price-day 19        # điền giá thuê $/ngày nếu muốn breakeven

# 5. Đọc kết quả
cat probench_report.md          # bảng + verdict
python3 -m json.tool probench_report.json | less
```

### Tuỳ chọn thường dùng

```bash
--scenarios probe,probe8k,chat,agentic,prefill   # chọn shape (mặc định: 4 shape, probe8k phải gọi tường minh)
--concurrency 1,4,8,16                           # thang concurrency
--miner-tax 2.3                                  # hệ số miner-path (đo lại cho CPU của bạn nếu có số)
--skip-hs-tax                                    # serve không bật hidden-states
--out /root/myreport                             # đổi chỗ ghi report
SERVE_PORT=8001 GPU_IDX=2 python3 sn53_probench.py ...   # serve cổng khác / GPU khác
```

### Shape mặc định (đo từ traffic thật của một fleet SN53, 7/2026)

| scenario | input | output | mô phỏng |
|---|---|---|---|
| probe | 12.900 | 4.096 | probe qualifier chính |
| probe8k | 12.900 | 8.192 | probe khi advertise out=8192 (opt-in) |
| chat | 1.024 | 512 | chat thường |
| agentic | 365 | 1.500 | buyer agentic prompt ngắn / completion dài |
| prefill | 30.000 | 256 | escalation prefill nặng |

## An toàn (quan trọng nếu box đang mining)

Tool **từ chối chạy khi phát hiện tiến trình miner đang sống** (supervisor hoặc
pgrep, fail-closed) — benchmark chen tải vào serve đang phục vụ traffic thật sẽ
phá TTFT/acceptance của worker. `--force` để bỏ qua khi bạn hiểu rõ rủi ro.

## Bài học đã nướng vào code (trả giá bằng tiền thuê GPU thật)

1. Sweep ngắn lạc quan ×3 — mọi verdict chỉ phát cho shape **đo trực tiếp**, không ngoại suy 4096→8192.
2. Gate chết người là **TTFT p99**, không chỉ deadline decode — PASS đòi cả hai.
3. Serve-only ≠ miner-path: hệ số ×2,3 (CPU-bound, đo trên EPYC Zen4; i9 nhẹ hơn) — override được.
4. Benchmark 1-wave PASS chưa chắc sống tải liên tục — đọc thêm cột GPU util/power để nhìn độ bão hoà.
5. Timeout giết cả process-group — không để orphan bench tiếp tục bắn tải.

## License

MIT
