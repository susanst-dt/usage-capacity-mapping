#!/usr/bin/env python3
"""
Usage-tier histogram: how many hours in a period each load level occurred.

Pulls hourly load (TPS or simultaneous users) per application from Dynatrace
via dtctl, buckets every hour into usage tiers, and renders a table + chart.

Examples
  python usage_tiers.py --context demo-live --period 31d   # or 7d, 2w, 12h; omit to be prompted
  python usage_tiers.py --service EasyTrade --metric tps --scale 60 --perf-test-users 200
  python usage_tiers.py --metric users --tiers "0-1:Nights/weekends,2-5:Early/evening,6-20:Moderate,21-:Peak"

Requires: dtctl (authenticated), python3, matplotlib  (pip install matplotlib)
"""
import argparse, csv, json, subprocess, sys
from collections import OrderedDict

# DQL per metric. Hourly buckets, one series per service.
# TPS   = peak-in-hour request rate (req/s)   -> dt.service.request.count
# users = RUM sessions (user.sessions): each session is spread over its start..end time,
#         giving concurrent sessions per time bin; the peak bin in each hour is used.
QUERIES = {
    "tps": ('timeseries v=sum(dt.service.request.count, rate:1s), by:{{dt.entity.service}}, '
            'interval:1h, from:-{period} '
            '| fieldsAdd name=entityName(dt.entity.service) {flt}'),
    "users": ('fetch user.sessions, from:-{period} {flt} '
              '| makeTimeseries v=count(), by:{{frontend.name}}, '
              'spread:timeframe(from:start_time, to:end_time), interval:5m, from:-{period}'),
}

DEFAULT_TIERS = "0-1:Nights & weekends,2-5:Early mornings / evenings,6-20:Moderate daytime,21-:Peak busy hours"


def parse_tiers(spec):
    tiers = []
    for part in spec.split(","):
        rng, label = part.split(":", 1)
        lo, hi = rng.split("-")
        tiers.append((float(lo), float(hi) if hi else float("inf"), label.strip()))
    return tiers


def tier_display(tier, unit="users"):
    """'Quiet (0-100 users)' / 'Peak (301+ users)'"""
    lo, hi, label = tier
    rng = f"{lo:g}+" if hi == float("inf") else f"{lo:g}-{hi:g}"
    return f"{label} ({rng} {unit})"


def tier_of(value, tiers):
    for lo, hi, label in tiers:
        if lo <= value <= hi or (hi != float("inf") and lo <= value < hi + 1):
            return label
    return tiers[-1][2]


PERIOD_UNITS = {"h": "h", "d": "d", "w": "w", "m": "d"}   # m = months -> 30 days each


def parse_period(text):
    """'7', '7d', '2w', '12h', '1m' -> ('7d', 7.0 days). A bare number means days."""
    import re
    m = re.fullmatch(r"\s*(\d+)\s*([hdwm]?)\s*", text.lower())
    if not m or int(m.group(1)) == 0:
        raise ValueError(f"cannot read '{text}' - use e.g. 24h, 7d, 2w, 1m")
    n, unit = int(m.group(1)), m.group(2) or "d"
    if unit == "m":
        n, unit = n * 30, "d"
    days = n / 24 if unit == "h" else n * 7 if unit == "w" else n
    return f"{n}{unit}", float(days)


def resolve_period(period_arg, days_arg=None, default="31d"):
    """Use --period (or legacy --days); otherwise ask when running in a terminal."""
    text = period_arg or (f"{days_arg}d" if days_arg else None)
    if text is None:
        if sys.stdin.isatty():
            while True:
                text = input(f"Time period to analyze (e.g. 24h, 7d, 2w, 1m) [{default}]: ").strip() or default
                try:
                    return parse_period(text)
                except ValueError as e:
                    print(e)
        text = default
    try:
        return parse_period(text)
    except ValueError as e:
        sys.exit(str(e))


def run_dql(context, dql):
    """Run a DQL query via dtctl and return its records.
    Tolerates different dtctl versions: records under payload["result"] or at the top level,
    and large results spilled to a JSONL file."""
    cmd = ["dtctl"] + (["--context", context] if context else []) + ["query", dql, "-o", "json"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        sys.exit(f"dtctl returned non-JSON output:\n{out.stdout[:800]}{out.stderr[:800]}")
    res = payload.get("result", payload) if isinstance(payload, dict) else {"records": payload}
    if isinstance(res, list):
        return res
    if "records" in res:
        return res["records"] or []
    if "path" in res:
        with open(res["path"]) as f:
            return [json.loads(line) for line in f if line.strip()]
    sys.exit(f"dtctl query failed (context '{context}'):\n{json.dumps(payload, indent=2)[:1500]}\n"
             "Try: dtctl auth login   (or check `dtctl config get-contexts`)")


def fetch(context, metric, period, name_filter):
    if metric == "users":
        flt = f'| filter contains(toString(frontend.name), "{name_filter}")' if name_filter else ""
    else:
        flt = f'| filter contains(name, "{name_filter}")' if name_filter else ""
    return run_dql(context, QUERIES[metric].format(period=period, flt=flt))


def aggregate(records, metric):
    """One load value per hour. TPS: sum across series. Users: sum concurrent sessions across
    series per bin, then take the peak bin within each hour (bins may be <1h, e.g. 30m)."""
    per_hour = max(1, round(3600e9 / int(records[0]["interval"])))
    n = max(len(r["v"]) for r in records)
    bins = [0.0] * n
    for r in records:
        for i, v in enumerate(r["v"]):
            bins[i] += v or 0.0
    return [max(bins[i:i + per_hour]) for i in range(0, n, per_hour)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", default="demo-live", help="dtctl context (demo environment)")
    ap.add_argument("--metric", choices=QUERIES, default="users",
                    help="users = concurrent RUM sessions (default); tps = service request rate")
    ap.add_argument("--period", default=None, help="time to analyze: 24h, 7d, 2w, 1m (bare number = days). Prompted if omitted")
    ap.add_argument("--days", type=int, default=None, help=argparse.SUPPRESS)  # legacy alias for --period
    ap.add_argument("--service", default="", help="substring of service/application name; blank = all")
    ap.add_argument("--tiers", default=DEFAULT_TIERS, help='"lo-hi:label,..." (blank hi = open ended)')
    ap.add_argument("--scale", type=float, default=1.0,
                    help="multiply load before bucketing (e.g. 60 to turn req/s into req/min)")
    ap.add_argument("--perf-test-users", type=float, default=None,
                    help="adds a reference row for the load level used in performance testing")
    ap.add_argument("--out", default="usage_tiers")
    a = ap.parse_args(argv)

    tiers = parse_tiers(a.tiers)
    period, period_days = resolve_period(a.period, a.days)
    recs = fetch(a.context, a.metric, period, a.service)
    if not recs:
        sys.exit("No data returned - check --service / --metric / --context.")
    hourly = [h * a.scale for h in aggregate(recs, a.metric)]

    counts = OrderedDict((t[2], 0) for t in tiers)
    for h in hourly:
        counts[tier_of(h, tiers)] += 1
    peak = max(hourly)

    # ---- table / csv ----
    unit = "TPS" if a.metric == "tps" else "users"
    print(f"\n{a.service or 'ALL'} | last {period} | {len(hourly)} hours | peak {peak:.2f} {unit}\n")
    rows = []
    for (lo, hi, label) in tiers:
        name = tier_display((lo, hi, label), unit)
        rows.append((name, counts[label]))
        print(f"{name:<40}{counts[label]:>6} h")
    if a.perf_test_users:
        rows.append((f"Performance test ({a.perf_test_users:g} {unit})", 0))
    with open(f"{a.out}.csv", "w", newline="") as f:
        csv.writer(f).writerows([("tier", "hours")] + rows)

    # ---- chart ----
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(13, 5), gridspec_kw={"width_ratios": [1.2, 1]})
    labels = [r[0] for r in rows][::-1]
    vals = [r[1] for r in rows][::-1]
    colors = ["#c4c9d4", "#7fb2e5", "#3d7cc9", "#b3261e", "#6b3fa0"][: len(tiers)]
    colors = (["#6b3fa0"] if a.perf_test_users else []) + colors[::-1]
    bars = ax.barh(labels, vals, color=colors[: len(labels)])
    for b, v in zip(bars, vals):
        ax.text(v + max(vals) * 0.01, b.get_y() + b.get_height() / 2, f"{v} h", va="center")
    ax.set_xlabel(f"Hours in the last {period}")
    ax.set_title(f"Hours at each load level ({unit})")
    ax.spines[["top", "right"]].set_visible(False)

    # load-duration curve: sorted hourly load, shows capacity needed vs. time at that level
    srt = sorted(hourly, reverse=True)
    ax2.fill_between(range(len(srt)), srt, color="#3d7cc9", alpha=.25)
    ax2.plot(srt, color="#3d7cc9")
    for lo, hi, label in tiers[1:]:
        ax2.axhline(lo, color="grey", ls=":", lw=.8)
    if a.perf_test_users:
        ax2.axhline(a.perf_test_users, color="#6b3fa0", ls="--")
        ax2.text(len(srt) * .98, a.perf_test_users, "perf test", ha="right", va="bottom", color="#6b3fa0")
    ax2.set_xlabel("Hours (ranked busiest to quietest)")
    ax2.set_ylabel(unit)
    ax2.set_title("Load-duration curve (capacity vs. time needed)")
    ax2.spines[["top", "right"]].set_visible(False)

    fig.suptitle(f"Usage pattern - {a.service or 'all services'} ({a.context})", fontweight="bold")
    fig.tight_layout()
    fig.savefig(f"{a.out}.png", dpi=150)
    print(f"\nWrote {a.out}.png and {a.out}.csv")


if __name__ == "__main__":
    main()
