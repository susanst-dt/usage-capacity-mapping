# Usage Tiers & Capacity Mapping

Two small scripts that answer: **"How many hours does this application run at each load level, and what do the servers behind it look like at those levels?"**

| Script | Question it answers |
|---|---|
| `usage_tiers.py` | How many hours in the period had 0–1 users, 2–5 users, 6–20 users, and so on? |
| `asset_capacity.py` | At each of those user levels, how busy are the EC2 instances (CPU and RAM) behind the application? |

Both read live data from Dynatrace using `dtctl`.

---

## What's in this package

| File | Purpose |
|---|---|
| `README.md` | This guide |
| `asset_capacity.py` | Main script: capacity (CPU/RAM) **and** usage tiers in one run |
| `usage_tiers.py` | Usage tiers only (also used by the main script, so keep it in the same folder) |
| `requirements.txt` | Python dependency (matplotlib) |
| `sample-output/` | Example results from the Dynatrace demo environment (EasyTrade, last 31 days, performance-test line at 600 users) |

Open the PNG files in `sample-output/` first to see what the results look like. The numbers in them come from the demo environment's synthetic traffic, so they show the format, not real usage patterns.

---

## 1. One-time setup

You need:
- **`dtctl`** installed and signed in to Dynatrace (`dtctl config get-contexts` should list your environment).
- **Python 3**.

From this folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Check that `dtctl` can reach the environment:

```bash
dtctl --context <your environment>> query 'fetch user.sessions, from:-1h | limit 1'
```

If that fails, run `dtctl auth login` and try again.

---

## 2. Run it

### Script 1: usage tiers (how busy is the application?)

```bash
.venv/bin/python usage_tiers.py --service EasyTrade
```

It asks which time period to analyze:

```
Time period to analyze (e.g. 24h, 7d, 2w, 1m) [31d]:
```

Type `7d`, `2w`, `1m` and so on, or press Enter for 31 days.

Common options:

| Option | What it does | Example |
|---|---|---|
| `--service` | Application name to analyze (part of the name is fine). Leave off for everything. | `--service EasyTrade` |
| `--period` | Skip the prompt. `24h`, `7d`, `2w`, `1m`. A bare number means days. | `--period 7d` |
| `--tiers` | Your own load levels, as `low-high:Label`. Leave `high` blank for "and above". | `--tiers "0-1:Nights,2-5:Early,6-20:Moderate,21-:Peak"` |
| `--metric` | `users` (default) counts concurrent user sessions. `tps` counts requests per second. | `--metric tps` |
| `--perf-test-users` | Adds a reference line for the load used in performance tests. | `--perf-test-users 200` |
| `--context` | Which Dynatrace environment to use. Default is `demo-live`. | `--context my-env` |

### Script 2: asset capacity (what do the servers look like?)

```bash
.venv/bin/python asset_capacity.py --frontend EasyTrade
```

`--frontend` is the application name as it appears in user monitoring (RUM). The script finds the servers behind it by looking for that name in the application's process names, tags and Kubernetes namespaces.

If it can't find the right servers (or finds extras), add `--app-tag` with a different search word, such as the namespace or process name. It's optional.

**This one command gives you both views.** After the CPU/RAM analysis it also runs the usage-tier analysis for the same application and period, so you get all four files from a single run. Add `--no-tiers` if you only want the CPU/RAM files, and `--perf-test-users` to put a performance-test line on the tier chart. You only need Script 1 on its own for applications that don't run on EC2, or to use `--metric tps`.

Extra options: `--period`, `--tiers` and `--context` work as above. You can also set ceilings with `--cpu-target` (default 70) and `--mem-target` (default 80). These are the CPU % and RAM % you consider "full".

### Full example

```bash
.venv/bin/python asset_capacity.py --frontend EasyTrade --period 31d --cpu-target 70 --mem-target 80 --tiers "0-100:Quiet,101-200:Moderate,201-300:Busy,301-:Peak"
```

> **Tip:** pick tiers that fit the application. The defaults (0–1, 2–5, 6–20, 21+) suit a small internal app. The demo environment sees hundreds of users, so use bigger numbers there.

---

## 3. What you get

`asset_capacity.py` writes all four files. `usage_tiers.py` writes only its own two. Files from an earlier run are replaced:

| File | Contents |
|---|---|
| `usage_tiers.png` / `.csv` | Hours per usage tier |
| `asset_capacity.png` / `.csv` | CPU and RAM per EC2 instance per usage tier |

---

## 4. How to read the data

### The key idea

Every hour in the period is given **one number**: the busiest moment in that hour. For users, that is the peak number of concurrent sessions. Each hour then falls into a tier (Quiet, Moderate, Busy, Peak). The tables count hours per tier.

### `usage_tiers` output

```
Quiet (0-100 users)         1 h
Moderate (101-200 users)    0 h
Busy (201-300 users)        2 h
Peak (301+ users)         742 h
```

- Each row says how many hours in the period had a peak in that range.
- The rows add up to the total hours analyzed (31 days is about 745 hours).
- **Left chart:** the same numbers as bars. A tall bar at the top tier means the application is busy most of the time.
- **Right chart (load-duration curve):** every hour sorted from busiest to quietest. Read it as: "the application needed at least *this many* users' worth of capacity for *this many* hours." A flat line means steady load. A steep drop means a short busy period and a long quiet one.
- A dashed purple line, if shown, is your performance-test load. If it sits far above the curve, you test well beyond real usage.

### `asset_capacity` output

**1. The hosts.** First it lists the EC2 instances running the application: instance id, host name, cores, and how many of the application's processes run there.

**2. CPU % and RAM % by usage tier.**

```
tier                        hours   c88c9a9c   f2892cc7
Peak (301+ users)             251      27.8%      53.1%
```

- `hours` is how many hours fell in that tier and had data.
- Each number under an instance is the **average CPU % (or RAM %)** of that whole server during those hours.
- A **dash** means no hours fell in that tier, so there is nothing to average.
- Compare across the tier rows. If CPU climbs as you move from Quiet to Peak, the users are driving that server. If it stays flat, something else is.

**3. Capacity estimate.**

```
i-06251282cf2892cc7  base 7.5%  +11.13% per 100 sessions  R2=0.45  -> 561 sessions
```

- **base** is the CPU (or RAM) when there are no users.
- **per 100 sessions** is how much CPU each extra 100 sessions adds.
- **-> N sessions** is the number of sessions at which that instance would hit your ceiling (70% CPU, 80% RAM by default).
- **R2** says how much to trust it, from 0 to 1:
  - Above 0.5: the users clearly drive the load, so the estimate is useful.
  - 0.3 to 0.5: a rough guide.
  - Below 0.3: the script prints "weak correlation". The server's load isn't following users, so **ignore the capacity number**.

**4. Charts.**
- **Left (bars):** average CPU and RAM per instance for each tier. The dashed line is your ceiling.
- **Right (dots):** each dot is one hour. The x-axis is users and the y-axis is CPU or RAM. A line sloping up means more users, more load. A flat line means no relationship.

---

## 5. Things to keep in mind

- **Sessions are not exactly people.** A session counts as active until it times out, so the numbers can run a little high. Treat them as an upper bound.
- **Host metrics cover the whole server.** If the server also runs other applications, their load is included. A host that sits at the same CPU at every user level is almost certainly busy with something else.
- **Some hours are skipped.** Only hours with both user data and host data are used, so the `hours` total in `asset_capacity` can be lower than in `usage_tiers`.
- **Flat data gives weak answers.** The capacity estimate needs load that rises and falls (for example, day versus night). The demo environment is close to steady, so expect low R2 values there. A real application with a daily pattern will give clearer results.
- **At least 24 hours of data** is needed for `asset_capacity`.

---

## 6. If something goes wrong

| Message | What to do |
|---|---|
| `no such file or directory: .venv/bin/python` | You are in the wrong folder. `cd` into this folder, then redo the setup in section 1. |
| `dtctl query failed` | Run `dtctl auth login`, then check `dtctl config get-contexts`. The message includes what `dtctl` returned. |
| `No data returned` | Check the `--service` / `--frontend` spelling, or try a longer `--period`. |
| `No processes found matching ...` | The name doesn't appear in any process name, tag or namespace. Add `--app-tag` with a word that does. |
| `... runs on N host(s) but none are EC2 instances` | The application runs somewhere other than AWS EC2 (for example Azure AKS). Only EC2 is supported for now. |
| `Too few overlapping hours` | Use a longer period, at least `24h`. |
| `ModuleNotFoundError: usage_tiers` | Keep `usage_tiers.py` and `asset_capacity.py` in the same folder. |
