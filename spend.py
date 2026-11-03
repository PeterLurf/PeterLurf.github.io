"""Adds up what I've spent on cloud GPUs and writes the totals to compute.json.

    python spend.py

GitHub runs this once a day (see .github/workflows/deploy.yml) with API keys kept as repository secrets,
so the keys never end up in the site: only the totals in compute.json do. build.py then writes those
totals into the homepage's Compute section.

The keys it reads (Settings > Secrets and variables > Actions on GitHub):
  VAST_API_KEY                            console.vast.ai > Account > API keys
  RUNPOD_API_KEY                          runpod.io > Settings > API keys (read-only is enough)
  MODAL_TOKEN_ID and MODAL_TOKEN_SECRET   modal.com > Settings > API tokens

A provider whose key is missing, or whose API fails, keeps the total already in compute.json,
so the site never drops to $0 because of a hiccup. You can also edit compute.json by hand.
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
SINCE = "2020-01-01"         # before my first GPU rental, so every charge is counted
MODAL_FREE_CREDIT = 30       # Modal covers the first $30 of each month on the free plan; that part isn't out of pocket


def get(url, key):
    # A named User-Agent: RunPod's Cloudflare turns away Python's default one (error 1010) before checking the key.
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                               "User-Agent": "peterlurf.github.io spend.py"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def vast(key):
    # Vast is prepaid: every dollar spent was first paid in, so add up the payments (credits), not the charges.
    q = urllib.parse.urlencode({"owner": "me", "sdate": 0, "edate": int(dt.datetime.now().timestamp()),
                                "inc_charges": "false"})
    rows = get(f"https://console.vast.ai/api/v0/users/me/invoices?{q}", key)["invoices"]
    return sum(abs(float(r["amount"])) for r in rows if r.get("type") == "payment")


def runpod(key):
    # Usage for Pods, serverless endpoints and network storage, one bucket per year.
    q = urllib.parse.urlencode({"bucketSize": "year", "startTime": f"{SINCE}T00:00:00Z",
                                "endTime": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return sum(float(r.get("amount") or 0)
               for kind in ("pods", "endpoints", "networkvolumes")
               for r in get(f"https://rest.runpod.io/v1/billing/{kind}?{q}", key))


def modal(_):
    import modal as m   # reads MODAL_TOKEN_ID and MODAL_TOKEN_SECRET from the environment
    # Modal reports go back only to the start of the month a year ago (older months can't be counted),
    # and a daily report covers at most 31 days, so ask for one month at a time.
    today = dt.date.today()
    month = max(dt.datetime.fromisoformat(SINCE), dt.datetime(today.year - 1, today.month, 1))
    months = defaultdict(float)
    while month.date() <= today:
        after = dt.datetime(month.year + month.month // 12, month.month % 12 + 1, 1)
        end = min(after, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))   # not into the future
        for r in m.billing.workspace_billing_report(start=month, end=end, resolution="d"):
            months[r["interval_start"].strftime("%Y-%m")] += float(r["cost"])
        month = after
    return sum(max(0.0, cost - MODAL_FREE_CREDIT) for cost in months.values())


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
    totals = data.get("providers", {})
    fetched = False
    for name, (fetch, env) in PROVIDERS.items():
        key = os.environ.get(env)
        if not key:
            print(f"{name}: no {env}, keeping ${totals.get(name, 0):,.2f}")
            continue
        try:
            totals[name] = round(fetch(key), 2)
            fetched = True
            print(f"{name}: ${totals[name]:,.2f}")
        except Exception as e:   # one provider's outage shouldn't stop the others or the deploy
            print(f"{name}: failed ({e}), keeping ${totals.get(name, 0):,.2f}")
    data["providers"] = totals
    if fetched:
        data["updated"] = dt.date.today().isoformat()
    DATA.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
