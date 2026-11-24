"""CLI dispatcher.

    python -m collect.run_all <source_name>     # run one collector
    python -m collect.run_all all               # run every enabled collector in turn
    python -m collect.run_all --list            # list sources

Each collector writes data/raw/<source>.jsonl.
"""
import argparse
import importlib
import json
import sys

from collect.common import SourceBlocked, load_config, source_cfg

SOURCES = ["play_store", "app_store", "trustpilot", "consumer_complaints", "youtube", "shareholder_letters"]


def run_one(name: str, cfg: dict) -> dict:
    sc = source_cfg(cfg, name)
    if not sc.get("enabled", False):
        return {"source": name, "status": "disabled"}
    mod = importlib.import_module("collect.%s" % name)
    try:
        summary = mod.run(cfg)
        summary.setdefault("status", "ok")
    except SourceBlocked as e:
        summary = {"source": name, "status": "blocked", "reason": str(e)}
    except Exception as e:  # report and carry on with other sources
        summary = {"source": name, "status": "error", "reason": "%s: %s" % (type(e).__name__, e)}
    summary.setdefault("source", name)
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Run a UC Discovery Engine collector.")
    p.add_argument("source", nargs="?", help="one of: %s, or 'all'" % ", ".join(SOURCES))
    p.add_argument("--list", action="store_true", help="list available sources")
    a = p.parse_args(argv)
    if a.list or not a.source:
        print("\n".join(SOURCES))
        return 0
    names = SOURCES if a.source == "all" else [a.source]
    bad = [n for n in names if n not in SOURCES]
    if bad:
        p.error("unknown source(s): %s" % ", ".join(bad))
    cfg = load_config()
    rc = 0
    for n in names:
        s = run_one(n, cfg)
        print(json.dumps(s, ensure_ascii=False))
        if s["status"] in ("blocked", "error"):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
