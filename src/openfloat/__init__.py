"""OpenFloat: an open protocol for financing at the payment layer."""

from __future__ import annotations

import argparse

from .messages import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION", "main"]


def _money(cents: float) -> str:
    return f"{cents / 100:,.0f}"


def main() -> None:
    parser = argparse.ArgumentParser(prog="openfloat", description="OpenFloat reference implementation")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sim = sub.add_parser("simulate", help="run a synthetic buyer/supplier/funder network and report outcomes")
    sim.add_argument("--seed", type=int, default=7)
    sim.add_argument("--days", type=int, default=30, help="days of invoice issuance")
    sim.add_argument("--no-forecast", action="store_true", help="buyer ignores payables it has not received yet")
    sim.add_argument("--greedy", action="store_true", help="use the greedy allocator instead of the MILP")
    sim.add_argument("--double-dip", type=float, default=0.05, help="share of suppliers that try to finance twice")
    sub.add_parser("dashboard", help="open the interactive dashboard in your browser")
    args = parser.parse_args()

    if args.cmd == "dashboard":
        import subprocess
        import sys
        import threading
        import webbrowser
        from pathlib import Path

        app = Path(__file__).with_name("dashboard.py")
        url = "http://localhost:8501"
        # headless skips Streamlit's interactive first-run email prompt; we open the browser ourselves
        print(f"OpenFloat dashboard: {url}  (Ctrl+C to stop)")
        threading.Timer(3.0, lambda: webbrowser.open(url)).start()
        cmd = [sys.executable, "-m", "streamlit", "run", str(app), "--server.headless", "true",
               "--server.port", "8501", "--browser.gatherUsageStats", "false"]
        raise SystemExit(subprocess.call(cmd))

    from .sim import ScenarioConfig, build, evaluate

    cfg = ScenarioConfig(
        seed=args.seed, days=args.days, use_forecast=not args.no_forecast,
        optimiser="greedy" if args.greedy else "milp", p_double_dip=args.double_dip,
    )
    sc = build(cfg)
    sc.run()
    m = evaluate(sc)
    print(f"{PROTOCOL_VERSION} | synthetic scenario seed={cfg.seed} | {len(sc.net.log)} signed messages\n")
    rows = [
        ("Invoices issued", f"{m.invoices}"),
        ("Early payment requested / financed", f"{m.early_requests} / {m.financed}"),
        ("Routes", ", ".join(f"{k}: {v}" for k, v in sorted(m.by_route.items()))),
        ("Face value financed", _money(m.face_financed)),
        ("Supplier cost via OpenFloat", _money(m.supplier_cost_openfloat)),
        ("Supplier cost via own factoring", _money(m.supplier_cost_factoring)),
        ("Supplier saving", _money(m.supplier_saving)),
        ("Buyer discount income, net of cash yield", _money(m.buyer_net_gain)),
        ("Double-financing attempts blocked", f"{m.double_financing_blocked}"),
        ("Messages rejected (bad signature)", f"{m.bad_signatures}"),
        ("Buyer min balance / floor", f"{_money(m.min_balance)} / {_money(m.floor)}"),
        ("Days below floor caused by early payment", f"{m.floor_breaches_caused}"),
    ]
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"  {k:<{width}}  {v}")
