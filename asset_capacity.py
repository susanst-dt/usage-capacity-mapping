#!/usr/bin/env python3
"""
Map user load to the infrastructure behind an application (EC2 instances).

1. Finds the hosts running the application's processes (Smartscape; matched on the frontend name)
   and their EC2 instance ids.
2. Pulls hourly concurrent RUM sessions (frontend) and hourly CPU and memory (RAM) % per host.
3. Shows CPU and RAM per usage tier per instance, fits each vs. users, and estimates the
   sessions each instance can carry at a target CPU / RAM level.

Example
  python asset_capacity.py --frontend EasyTrade --cpu-target 70 \
      --tiers "0-100:Quiet,101-200:Moderate,201-300:Busy,301-:Peak"

One run also writes the usage-tier chart (usage_tiers.png/.csv) unless --no-tiers is given.
Reuses usage_tiers.py (same folder). Requires dtctl, matplotlib.
"""
import argparse, csv, os, sys
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # find usage_tiers.py
import usage_tiers as ut


def dql(context, query):
    return ut.run_dql(context, query)


def find_assets(context, term):
    """EC2-backed hosts running the app: [{host, host_id, instance_id, cores, cluster}].
    A process belongs to the app if `term` appears (case-insensitive) in its application tag,
    its name, or its Kubernetes namespace."""
    t = term.lower().replace('"', "")
    procs = dql(context, 'smartscapeNodes "PROCESS" '
                         '| filter contains(lower(toString(primary_tags.application)), "%s") '
                         'or contains(lower(name), "%s") or contains(lower(toString(k8s.namespace.name)), "%s") '
                         '| summarize procs=count(), by:{host.name, aws.arn, k8s.cluster.name}' % (t, t, t))
    if not procs:
        sys.exit(f'No processes found matching "{term}". Try --app-tag with part of the application\'s '
                 'process, namespace or tag name.')
    arns = [p["aws.arn"] for p in procs if p.get("aws.arn")]
    if not arns:
        others = ", ".join(sorted({p["host.name"] for p in procs}))
        sys.exit(f'"{term}" runs on {len(procs)} host(s) but none are EC2 instances: {others}\n'
                 "This script maps to EC2 only (other platforms such as Azure/AKS are not supported yet).")
    hosts = dql(context, 'smartscapeNodes "HOST" | filter in(aws.arn, array(%s)) | fields id, name, cores, aws.arn'
                % ",".join(f'"{a}"' for a in arns))
    by_arn = {h["aws.arn"]: h for h in hosts}
    assets = {}
    for p in procs:
        h = by_arn.get(p.get("aws.arn"))
        if h:
            s = assets.setdefault(h["id"], dict(host=h["name"], host_id=h["id"], cores=h.get("cores", "?"),
                                                instance_id=p["aws.arn"].split("/")[-1],
                                                cluster=p.get("k8s.cluster.name") or "", procs=0))
            s["procs"] += int(p["procs"])
    return list(assets.values())


def hourly_by_start(timeframe_start, values):
    """{hour_start(datetime): value}"""
    t0 = datetime.fromisoformat(timeframe_start.replace("Z", "+00:00")[:19] + "+00:00")
    return {t0.timestamp() + 3600 * i: v for i, v in enumerate(values)}


# metric key -> (DQL metric, display name)
METRICS = {"cpu": ("dt.host.cpu.usage", "CPU"), "mem": ("dt.host.memory.usage", "RAM")}


def fetch_metric(context, assets, period, key):
    ids = ",".join(f'"{a["host_id"]}"' for a in assets)
    recs = dql(context, 'timeseries v=avg(%s), by:{dt.entity.host}, interval:1h, from:-%s, '
                        'filter: in(dt.entity.host, array(%s))' % (METRICS[key][0], period, ids))
    return {r["dt.entity.host"]: hourly_by_start(r["timeframe"]["start"], r["v"]) for r in recs}


def fit(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return my, 0.0, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys)) / ss_tot if ss_tot else 0.0
    return a, b, r2


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", default="demo-live")
    ap.add_argument("--frontend", required=True, help="RUM frontend name substring, e.g. EasyTrade")
    ap.add_argument("--app-tag", default=None,
                    help="optional: name to match the backing processes (tag, process name or namespace); defaults to --frontend")
    ap.add_argument("--period", default=None, help="time to analyze: 24h, 7d, 2w, 1m (bare number = days). Prompted if omitted")
    ap.add_argument("--days", type=int, default=None, help=argparse.SUPPRESS)  # legacy alias for --period
    ap.add_argument("--tiers", default=ut.DEFAULT_TIERS)
    ap.add_argument("--cpu-target", type=float, default=70.0, help="CPU %% regarded as the capacity ceiling")
    ap.add_argument("--mem-target", type=float, default=80.0, help="RAM %% regarded as the capacity ceiling")
    ap.add_argument("--out", default="asset_capacity")
    ap.add_argument("--no-tiers", action="store_true", help="skip the usage-tier chart (usage_tiers.png/.csv)")
    ap.add_argument("--perf-test-users", type=float, default=None, help="reference load line on the usage-tier chart")
    a = ap.parse_args()
    target = {"cpu": a.cpu_target, "mem": a.mem_target}

    tiers = ut.parse_tiers(a.tiers)
    period, _ = ut.resolve_period(a.period, a.days)
    assets = find_assets(a.context, a.app_tag or a.frontend)
    print(f"\n{len(assets)} EC2 hosts behind '{a.app_tag or a.frontend}':")
    for s in assets:
        print(f"  {s['instance_id']:<21} {s['host']:<34} {s['cores']} cores  {s['procs']} procs  {s['cluster']}")

    # hourly users, keyed by hour start so it lines up with host metrics
    urecs = ut.fetch(a.context, "users", period, a.frontend)
    step = int(urecs[0]["interval"]) / 1e9                  # seconds per bin (bins may start at :30)
    t0 = next(iter(hourly_by_start(urecs[0]["timeframe"]["start"], [0])))
    bins = [0.0] * max(len(r["v"]) for r in urecs)
    for r in urecs:
        for i, v in enumerate(r["v"]):
            bins[i] += v or 0
    users = {}
    for i, v in enumerate(bins):                            # peak bin within each clock hour
        hr = (t0 + i * step) // 3600 * 3600
        users[hr] = max(users.get(hr, 0), v)
    data = {k: fetch_metric(a.context, assets, period, k) for k in METRICS}

    def valid(s, k):                    # hours where this host has data for metric k (hosts come and go)
        c = data[k].get(s["host_id"], {})
        return sorted(h for h in users if c.get(h) is not None)
    hrs = {(s["host_id"], k): valid(s, k) for s in assets for k in METRICS}
    hours = sorted(set().union(*hrs.values()))
    if len(hours) < 24:
        sys.exit("Too few overlapping hours between sessions and host metrics.")
    u = [users[h] for h in hours]
    tier_names = [t[2] for t in tiers]
    shown = {t[2]: ut.tier_display(t) for t in tiers}       # "Quiet (0-100 users)"
    tier_hours = {n: [h for h, x in zip(hours, u) if ut.tier_of(x, tiers) == n] for n in tier_names}

    grid, fits, csv_rows = {}, {}, []
    for k, (_, name) in METRICS.items():
        d = data[k]
        print(f"\nAverage {name} % by usage tier ({len(hours)} hours with sessions + host data, last {period})")
        print("(a dash means no hours fell in that tier, so there is nothing to average)")
        print(f"{'tier':<34}{'hours':>6}" + "".join(f"{s['instance_id'][-8:]:>11}" for s in assets))
        for n in tier_names:
            hs = tier_hours[n]
            vals = []
            for s in assets:
                c = [d[s["host_id"]][h] for h in hs if d[s["host_id"]].get(h) is not None]
                vals.append(sum(c) / len(c) if c else float("nan"))
            grid[(k, n)] = vals
            print(f"{shown[n]:<34}{len(hs):>6}" + "".join(f"{'-':>11}" if v != v else f"{v:>10.1f}%" for v in vals))
            csv_rows.append([name, shown[n], len(hs)] + ["" if v != v else f"{v:.1f}" for v in vals])

        print(f"\nCapacity at {target[k]:g}% {name} (linear fit of hourly {name} vs. concurrent sessions)")
        for s in assets:
            hh = hrs[(s["host_id"], k)]
            a0, b, r2 = fit([users[h] for h in hh], [d[s["host_id"]][h] for h in hh])
            fits[(s["host_id"], k)] = (a0, b, r2)
            cap = (target[k] - a0) / b if b > 1e-9 else float("inf")
            note = "" if r2 >= 0.3 else f"  (weak correlation - {name} not driven by sessions)"
            capstr = f"{cap:,.0f} sessions" if cap != float("inf") else "n/a"
            print(f"  {s['instance_id']:<21} base {a0:5.1f}%  +{b*100:5.2f}% per 100 sessions  R2={r2:.2f}  -> {capstr}{note}")

    with open(f"{a.out}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "tier", "hours"] + [s["instance_id"] for s in assets])
        w.writerows(csv_rows)

    # ---- chart: one row per metric (CPU, RAM); left = by tier, right = vs. users ----
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    pal = ["#3d7cc9", "#e08a1e", "#2a9d8f", "#b3261e", "#6b3fa0", "#7a7f8a"]
    fig, axes = plt.subplots(len(METRICS), 2, figsize=(14, 5 * len(METRICS)))
    width = 0.8 / len(assets)
    for row, (k, (_, name)) in enumerate(METRICS.items()):
        ax, ax2 = axes[row]
        for j, s in enumerate(assets):
            ax.bar([i + j * width for i in range(len(tier_names))],
                   [0 if grid[(k, n)][j] != grid[(k, n)][j] else grid[(k, n)][j] for n in tier_names],
                   width, label=f"{s['instance_id']} ({s['cores']} cores)", color=pal[j % len(pal)])
        ax.axhline(target[k], color="grey", ls="--", lw=1)
        ax.set_xticks([i + 0.4 - width / 2 for i in range(len(tier_names))])
        ax.set_xticklabels([f"{shown[n].replace(' (', chr(10) + '(')}\n{len(tier_hours[n])} h" for n in tier_names], fontsize=8)
        ax.set_ylabel(f"Average {name} %"); ax.set_title(f"{name} per EC2 instance by usage tier")
        ax.set_ylim(0, 100)
        if row == 0:
            ax.legend(fontsize=7, loc="upper left")
        ax.spines[["top", "right"]].set_visible(False)
        for j, s in enumerate(assets):
            hh = hrs[(s["host_id"], k)]
            ax2.scatter([users[h] for h in hh], [data[k][s["host_id"]][h] for h in hh], s=8, alpha=.4, color=pal[j % len(pal)])
            a0, b, _ = fits[(s["host_id"], k)]
            lo, hi = min(u), max(u)
            ax2.plot([lo, hi], [a0 + b * lo, a0 + b * hi], color=pal[j % len(pal)], lw=1.5)
        ax2.axhline(target[k], color="grey", ls="--", lw=1)
        ax2.set_xlabel("Concurrent RUM sessions (hourly peak)"); ax2.set_ylabel(f"{name} %")
        ax2.set_title(f"{name} vs. users (each dot = 1 hour)"); ax2.spines[["top", "right"]].set_visible(False)
    fig.suptitle(f"{a.frontend} users mapped to EC2 capacity ({a.context})", fontweight="bold")
    fig.tight_layout(); fig.savefig(f"{a.out}.png", dpi=130)
    print(f"\nWrote {a.out}.png and {a.out}.csv")

    if not a.no_tiers:                                     # same run also produces the usage-tier view
        args = ["--context", a.context, "--metric", "users", "--service", a.frontend,
                "--period", period, "--tiers", a.tiers]
        if a.perf_test_users:
            args += ["--perf-test-users", str(a.perf_test_users)]
        print("\n=== Usage tiers ===")
        ut.main(args)


if __name__ == "__main__":
    main()
