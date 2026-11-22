#!/usr/bin/env python3
"""Verify source wiring between runtime pipeline creation and durable list saving.

This is an integration guard for code that needs the real NVN/Vulkan engine to
compile: every cacheable graphics-pipeline record enters the list autosave even
when its compile is faster than the driver-cache 2 ms accounting threshold.
"""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
game = (root / "xbg_game.cpp").read_text(encoding="utf-8")
header = (root / "xbg_game.h").read_text(encoding="utf-8")


def need(text: str, label: str) -> None:
    if text not in game and text not in header:
        raise SystemExit(f"FAIL: missing {label}: {text}")


need('#include "xbg_pipeline_warm_store.h"', "durable store include")
need("xbg_pipeline_warm_store::Autosave list_autosave;", "autosave owner")
need("list_autosave.append(std::move(encoded))", "completed-record enqueue")
need('persist.debounce=std::chrono::milliseconds(2000);', "2 s real-time debounce")
need("void request_list_flush(){list_autosave.request_now();}", "non-blocking flush request")
need("bool wait_list_flushed(uint32_t timeout_ms)", "bounded list-only wait")
need("void xbg_game_request_pipeline_list_save(void);", "public request API")
need("int xbg_game_wait_pipeline_list_save(uint32_t timeout_ms);", "public wait API")
need("warm::request_list_flush();warm::wait_list_flushed(2000);", "shutdown final request/wait")
need("list_autosave.stop();", "worker shutdown join")

# Runtime graphics creation always calls record() after a successful cacheable
# miss.  The 2 ms test below it controls only the driver-cache dirty counter and
# must not gate the portable list record.
lookup = game.index("void pipeline_lookup(")
after_lookup = game.index("void clear_pipeline_objects(){", lookup)
lookup_body = game[lookup:after_lookup]
record_call = "if(cacheable)warm::record(*graphics,key);"
fast_compile = "if(dt>=compile_ns)++pipelines_unsaved;"
if record_call not in lookup_body or fast_compile not in lookup_body:
    raise SystemExit("FAIL: runtime pipeline record/timing wiring changed")
if lookup_body.index(record_call) < lookup_body.index(fast_compile):
    raise SystemExit("FAIL: driver-cache timing unexpectedly gates the portable record")

# The list must never be flushed through the driver-cache busy path again.
save = game[game.index("void save_pipeline_cache(){"):game.index("extern bool tex_dirty;")]
if "warm::flush" in save or "list_autosave" in save:
    raise SystemExit("FAIL: driver-cache writer still couples to list autosave")

print("PASS: durable list autosave source wiring; fast pipelines enqueue independently of >2 ms/driver-cache busy state")
