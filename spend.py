"""Keeps a month-by-month record of what I've spent on cloud GPUs, in compute.json.

    python spend.py

GitHub runs this once a day (see .github/workflows/deploy.yml) with API keys kept as repository secrets,
so the keys never end up in the site: only the monthly amounts in compute.json do. build.py then adds
them up for the homepage's Compute section.

The record lives on the repository's `data` branch, not on main, so the daily update never gets in the
way of your own pushes. Each run asks every provider for all the months it still has and updates those;
months a provider no longer reports (Modal keeps about a year) stay as they were, so the record keeps
the full history even after a provider forgets it. To add spending from before (or from a provider with
no API), edit compute.json on the data branch on GitHub: "months" > provider > "YYYY-MM": dollars.

The keys it reads (Settings > Secrets and variables > Actions on GitHub):
  VAST_API_KEY or VAST                    console.vast.ai > Account > API keys
  RUNPOD_API_KEY or RUNPOD                runpod.io > Settings > API keys (read-only is enough)
  MODAL_TOKEN_ID and MODAL_TOKEN_SECRET   modal.com > Settings > API tokens
    (or both in one MODAL secret)

A provider whose key is missing, or whose API fails, keeps its months as they are.
"""
import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent / "compute.json"
SINCE = dt.date(2022, 1, 1)   # before my first GPU rental, so every charge is counted


def get(url, key):
    # A named User-Agent: RunPod's Cloudflare turns away Python's default one (error 1010) before checking the key.
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                               "User-Agent": "peterlurf.github.io spend.py"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def vast(key):
    # Vast is prepaid: every dollar spent was first paid in, so count the payments (credits), not the charges.
    q = urllib.parse.urlencode({"owner": "me", "sdate": 0, "edate": int(dt.datetime.now().timestamp()),
                                "inc_charges": "false"})
    months = defaultdict(float)
    for r in get(f"https://console.vast.ai/api/v0/users/me/invoices?{q}", key)["invoices"]:
        if r.get("type") == "payment":
            when = dt.datetime.fromtimestamp(float(r.get("timestamp") or 0), dt.timezone.utc)
            months[f"{when:%Y-%m}"] += abs(float(r["amount"]))
    return months


def runpod(key):
    # Usage for Pods, serverless endpoints and network storage, one bucket per month.
    q = urllib.parse.urlencode({"bucketSize": "month", "startTime": f"{SINCE}T00:00:00Z",
                                "endTime": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    months = defaultdict(float)
    for kind in ("pods", "endpoints", "networkvolumes"):
        for r in get(f"https://rest.runpod.io/v1/billing/{kind}?{q}", key):
            months[r["time"][:7]] += float(r.get("amount") or 0)
    return months


def modal(_):
    import modal as m   # reads MODAL_TOKEN_ID and MODAL_TOKEN_SECRET from the environment
    # One summary per monthly billing cycle. billed_cost is what Modal actually charged, after the
    # plan's free credits, so it's the out-of-pocket part. Months it no longer keeps raise an error.
    billing = m.Workspace.from_context().billing
    today = dt.date.today()
    months, errors = {}, []
    for i in range(SINCE.year * 12 + SINCE.month - 1, today.year * 12 + today.month):
        cycle = f"{i // 12}-{i % 12 + 1:02d}"
        try:
            months[cycle] = float(billing.summary(cycle=cycle).billed_cost)
        except Exception as e:
            errors.append(e)
    if not months:
        raise errors[-1]
    return months


PROVIDERS = {"Vast.ai": (vast, "VAST_API_KEY"), "RunPod": (runpod, "RUNPOD_API_KEY"), "Modal": (modal, "MODAL_TOKEN_ID")}


def modal_tokens():
    """Both Modal tokens can also come in one MODAL secret: token IDs start with ak-, secrets with as-."""
    both = os.environ.get("MODAL", "")
    for env, prefix in (("MODAL_TOKEN_ID", "ak-"), ("MODAL_TOKEN_SECRET", "as-")):
        m = re.search(rf"\b{prefix}[A-Za-z0-9]+", both)
        if m and not os.environ.get(env):
            os.environ[env] = m.group(0)
        elif both and not m:
            print(f"Modal: the MODAL secret has no {prefix}... token in it")


def main():
    modal_tokens()
    data = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {}
    record = data.get("months", {})
    fetched = False
    for name, (fetch, env) in PROVIDERS.items():
        mine = record.setdefault(name, {})
        if not os.environ.get(env):
            print(f"{name}: no {env}, keeping ${sum(mine.values()):,.2f}")
            continue
        try:
            got = {k: round(v, 2) for k, v in fetch(os.environ[env]).items() if v}
            mine.update(got)
            fetched = True
            span = f"{min(got)} to {max(got)}" if got else "no spending"
            print(f"{name}: reported {span}; ${sum(mine.values()):,.2f} over {len(mine)} month(s) on record")
        except Exception as e:   # one provider's outage shouldn't stop the others or the deploy
            print(f"{name}: failed ({e}), keeping ${sum(mine.values()):,.2f}")
        record[name] = dict(sorted(mine.items()))
    data = {"updated": dt.date.today().isoformat() if fetched else data.get("updated"), "months": record}
    DATA.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
