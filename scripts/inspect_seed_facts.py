import datetime
import json
from pathlib import Path


def parse_dt(s: str | None) -> datetime.datetime:
    if not s:
        return datetime.datetime.min
    try:
        # seed timestamps are often ISO; handle trailing Z.
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.datetime.min


def get_latest_merged_facts(seed_path: Path, app_id: str) -> dict:
    latest_by_key = {}
    latest_dt_by_key = {}

    with seed_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if ev.get("event_type") != "ExtractionCompleted":
                continue
            payload = ev.get("payload") or {}
            if payload.get("package_id") != app_id:
                continue
            facts = payload.get("facts") or {}
            dt = parse_dt(ev.get("recorded_at"))

            for k, v in facts.items():
                if v is None:
                    continue
                prev_dt = latest_dt_by_key.get(k, datetime.datetime.min)
                if dt >= prev_dt:
                    latest_by_key[k] = v
                    latest_dt_by_key[k] = dt

    return latest_by_key


def main() -> None:
    seed_path = Path("data/seed_events.jsonl")
    apps = ["APEX-0022", "APEX-0028", "APEX-0023", "APEX-0001"]
    keys = [
        "debt_to_equity",
        "debt_to_ebitda",
        "current_ratio",
        "net_margin",
        "gaap_compliant",
        "balance_discrepancy_usd",
        "interest_coverage",
        "total_revenue",
        "net_income",
        "operating_cash_flow",
    ]

    for app in apps:
        facts = get_latest_merged_facts(seed_path, app)
        print(app)
        print({k: facts.get(k) for k in keys})
        print("---")


if __name__ == "__main__":
    main()

