"""engy hidden-states trim — put THIS folder on the sglang serve's PYTHONPATH.

Rebuilt 2026-07-24 after the 0.5.15 adaptation was lost with the old checkout.
Upstream's miner/sitecustomize.py targets sglang 0.5.12 only (it imports
`scheduler_output_processor_mixin`, which 0.5.15 no longer has) and fails
closed — "not armed" — leaving the serve returning the FULL prefill hidden-state
block, which is the parse choke that killed throughput before (I-13/I-15).

This version resolves each wrap against a list of candidate locations, arms each
one independently, and reports exactly which target it bound to. It never raises
at interpreter startup.

NEVER put this on a validator serve — the recompute reads the full prefill block.
"""
import importlib
import sys

_PREFILL_METHOD = "process_batch_result_prefill"

# Newest first. 0.5.15 moved prefill post-processing into scheduler_components;
# 0.5.12 had it on the mixin.
_PREFILL_TARGETS = [
    ("sglang.srt.managers.scheduler_components.batch_result_processor",
     "SchedulerBatchResultProcessor"),
    ("sglang.srt.managers.scheduler_output_processor_mixin",
     "SchedulerOutputProcessorMixin"),
    ("sglang.srt.managers.scheduler", "Scheduler"),
]

_OUTPUT_TARGETS = [
    ("sglang.srt.managers.io_struct", "BatchTokenIDOutput"),
    ("sglang.srt.managers.io_struct", "BatchTokenIDOut"),
]


class _LastRowProxy:
    """Slice `t[a:b]` (one request's prompt rows) returns only the last row."""

    __slots__ = ("_t",)

    def __init__(self, t):
        self._t = t

    def __getitem__(self, k):
        if isinstance(k, slice) and k.step is None and k.stop is not None:
            # Clamp to the tensor end first: under chunked prefill it holds only
            # the last chunk's rows, fewer than prompt_len.
            stop = min(k.stop, self._t.shape[0])
            start = max(stop - 1, 0 if k.start is None else k.start)
            return self._t[start:stop]
        return self._t[k]

    def __getattr__(self, name):
        return getattr(self._t, name)

    def __len__(self):
        return len(self._t)


def _install_skip_prefill():
    """Replace the prefill block with a proxy that yields only its last row —
    the one hidden state that sampled output token 0. Returns the bound target."""
    for mod_name, cls_name in _PREFILL_TARGETS:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        cls = getattr(mod, cls_name, None)
        orig = getattr(cls, _PREFILL_METHOD, None) if cls is not None else None
        if orig is None or getattr(orig, "_engy_wrapped", False):
            continue

        def wrapped(self, *args, _orig=orig, **kwargs):
            for a in list(args) + list(kwargs.values()):
                lo = getattr(a, "logits_output", None)
                hs = getattr(lo, "hidden_states", None) if lo is not None else None
                if hs is not None and not isinstance(hs, _LastRowProxy):
                    lo.hidden_states = _LastRowProxy(hs)
            return _orig(self, *args, **kwargs)

        wrapped._engy_wrapped = True
        try:
            setattr(cls, _PREFILL_METHOD, wrapped)
        except (TypeError, AttributeError):
            continue
        if getattr(cls, _PREFILL_METHOD, None) is not wrapped:
            continue                      # slotted/immutable class: not armed
        return f"{mod_name}.{cls_name}"
    return None


def _install_only_last_hidden():
    """Blank the accumulated per-step hidden-state copies, keeping only the
    finish step's — the receiver overwrites meta_info["hidden_states"] on every
    recv, so the earlier copies are pure transport cost. Returns the target.

    In 0.5.15 the output class is a msgspec.Struct; some builds expose a C-level
    __init__ that cannot be replaced, so the assignment is verified, not assumed.
    """
    for mod_name, cls_name in _OUTPUT_TARGETS:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        cls = getattr(mod, cls_name, None)
        if cls is None:
            continue
        orig_init = getattr(cls, "__init__", None)
        if orig_init is None or getattr(orig_init, "_engy_wrapped", False):
            continue

        def wrapped_init(self, *args, _orig=orig_init, **kwargs):
            _orig(self, *args, **kwargs)
            hs = getattr(self, "output_hidden_states", None)
            fr = getattr(self, "finished_reasons", None)
            # Only when the list aligns 1:1 with the finish reasons — otherwise
            # slots cannot be matched and blanking would drop a live row.
            if hs and fr and len(hs) == len(fr):
                try:
                    self.output_hidden_states = [
                        h if f is not None else [] for h, f in zip(hs, fr)
                    ]
                except Exception:
                    pass                  # frozen struct: leave it alone

        wrapped_init._engy_wrapped = True
        try:
            cls.__init__ = wrapped_init
        except (TypeError, AttributeError):
            continue
        if cls.__init__ is not wrapped_init:
            continue                      # C-level init: not replaceable
        return f"{mod_name}.{cls_name}"
    return None


_armed = []
for _name, _fn in (("skip_prefill", _install_skip_prefill),
                   ("only_last_hidden", _install_only_last_hidden)):
    try:
        _where = _fn()
    except Exception as e:               # never break interpreter startup
        _where, e = None, e
        print(f"[engy] hidden-states trim {_name} FAILED: {e!r}", file=sys.stderr,
              flush=True)
        continue
    if _where:
        _armed.append(f"{_name}@{_where}")
    else:
        print(f"[engy] hidden-states trim {_name} NOT armed — no known target "
              f"matched this sglang build; run inspect_sglang.py and adapt",
              file=sys.stderr, flush=True)

if _armed:
    # Keep the exact phrase "trim armed": every runbook greps for it.
    print("[engy] hidden-states trim armed (" + ", ".join(_armed) + ")", flush=True)
