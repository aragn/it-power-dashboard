"""
etl/fetch_pun.py

Pulls the PUN Index GME from the GME API and writes
app/data/pun.json in the schema the frontend expects:

{
    "source": "GME - PUN Index GME (MGP)",
    "series": [
        {"date": "YYYY-MM-DD", "pun": float},
        ...
    ]
}

The GME API is queried in monthly chunks to avoid request-size limits.
The script also handles GME rate limiting (HTTP 429) with automatic retries.

Credentials:
    GME_API_LOGIN
    GME_API_PASSWORD

Example:
    py etl/fetch_pun.py --start 20250101 --end 20260924
"""

import argparse
import base64
import io
import json
import os
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta

import requests


API_BASE = "https://api.mercatoelettrico.org/request"

OUT_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "app",
    "data",
    "pun.json",
)


def get_token(login: str, password: str) -> str:
    """Authenticate with GME and return the JWT token."""

    resp = requests.post(
        f"{API_BASE}/api/v1/Auth",
        json={
            "Login": login,
            "Password": password,
        },
    )

    resp.raise_for_status()

    payload = resp.json()

    # GME currently returns lowercase response fields.
    if not payload.get("success"):
        raise RuntimeError(
            f"GME auth failed. Response from GME: {payload}"
        )

    return payload["token"]


def request_data(token: str, interval_start: str, interval_end: str) -> list:
    """
    Request GME data in monthly chunks.

    This avoids the HTTP 412 error encountered when requesting
    a very large date range in a single API call.

    If GME returns HTTP 429 (Too Many Requests), the request is
    retried with exponential backoff.
    """

    start_date = datetime.strptime(
        interval_start,
        "%Y%m%d",
    ).date()

    end_date = datetime.strptime(
        interval_end,
        "%Y%m%d",
    ).date()

    all_rows = []

    current_date = start_date

    while current_date <= end_date:

        # Calculate the first day of the next month.
        if current_date.month == 12:
            next_month = current_date.replace(
                year=current_date.year + 1,
                month=1,
                day=1,
            )
        else:
            next_month = current_date.replace(
                month=current_date.month + 1,
                day=1,
            )

        # Last day of the current month,
        # or the requested end date if earlier.
        chunk_end = min(
            end_date,
            next_month - timedelta(days=1),
        )

        chunk_start_str = current_date.strftime("%Y%m%d")
        chunk_end_str = chunk_end.strftime("%Y%m%d")

        print(
            f"Requesting GME data: "
            f"{chunk_start_str} -> {chunk_end_str}"
        )

        body = {
            "Platform": "PublicMarketResults",
            "Segment": "MGP",
            "DataName": "ME_ZonalPrices",
            "IntervalStart": chunk_start_str,
            "IntervalEnd": chunk_end_str,
            "Attributes": {},
        }

        max_retries = 6

        for attempt in range(max_retries):

            resp = requests.post(
                f"{API_BASE}/api/v1/RequestData",
                headers={
                    "Authorization": f"Bearer {token}"
                },
                json=body,
            )

            # Successful request or an error other than 429.
            if resp.status_code != 429:
                break

            # GME may tell us how long to wait.
            retry_after = resp.headers.get("Retry-After")

            if retry_after:
                try:
                    wait_seconds = int(retry_after)
                except ValueError:
                    wait_seconds = 5 * (2 ** attempt)
            else:
                # Exponential backoff:
                # 5, 10, 20, 40, 80, 160 seconds
                wait_seconds = 5 * (2 ** attempt)

            print(
                f"  GME rate limit reached. "
                f"Waiting {wait_seconds} seconds "
                f"before retry "
                f"({attempt + 1}/{max_retries})..."
            )

            time.sleep(wait_seconds)

        # Raise an error if the request still failed.
        resp.raise_for_status()

        payload = resp.json()

        # GME currently returns lowercase response fields,
        # but support both versions.
        result_request = payload.get(
            "resultRequest",
            payload.get("ResultRequest"),
        )

        content_response = payload.get(
            "contentResponse",
            payload.get("ContentResponse"),
        )

        if result_request and result_request != "OK":
            raise RuntimeError(
                f"GME data request failed for "
                f"{chunk_start_str}-{chunk_end_str}: "
                f"{payload}"
            )

        if not content_response:
            # Avoid printing credentials or tokens.
            safe_payload = {
                key: value
                for key, value in payload.items()
                if key.lower() != "token"
            }

            raise RuntimeError(
                f"GME returned no data content for "
                f"{chunk_start_str}-{chunk_end_str}: "
                f"{safe_payload}"
            )

        # GME returns the data as a base64-encoded ZIP file.
        raw_zip = base64.b64decode(content_response)

        with zipfile.ZipFile(
            io.BytesIO(raw_zip)
        ) as zf:

            json_name = next(
                name
                for name in zf.namelist()
                if name.endswith(".json")
            )

            with zf.open(json_name) as f:
                rows = json.load(f)

        all_rows.extend(rows)

        print(
            f"  Received {len(rows)} rows"
        )

        # Give GME some breathing room before the next request.
        time.sleep(5)

        # Move to the next month.
        current_date = chunk_end + timedelta(days=1)

    print(
        f"Total rows received: {len(all_rows)}"
    )

    return all_rows


def to_daily_pun(rows: list) -> list:
    """
    Filter to Zone == 'PUN' and calculate one daily PUN value.

    All PUN price observations belonging to the same FlowDate
    are averaged.
    """

    by_day = defaultdict(list)

    for row in rows:

        if row.get("Zone") != "PUN":
            continue

        flow_date = str(row["FlowDate"])

        by_day[flow_date].append(
            float(row["Price"])
        )

    out = []

    for flow_date, prices in sorted(
        by_day.items()
    ):

        dt = datetime.strptime(
            flow_date,
            "%Y%m%d",
        ).date().isoformat()

        out.append(
            {
                "date": dt,
                "pun": round(
                    sum(prices) / len(prices),
                    2,
                ),
            }
        )

    return out


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--start",
        required=True,
        help="yyyyMMdd",
    )

    parser.add_argument(
        "--end",
        required=True,
        help="yyyyMMdd",
    )

    args = parser.parse_args()

    # Read GME credentials from environment variables.
    login = os.environ["GME_API_LOGIN"]
    password = os.environ["GME_API_PASSWORD"]

    # Authenticate.
    token = get_token(
        login,
        password,
    )

    # Download GME data.
    rows = request_data(
        token,
        args.start,
        args.end,
    )

    # Convert hourly/period data into daily PUN.
    daily = to_daily_pun(rows)

    # Make sure the output directory exists.
    os.makedirs(
        os.path.dirname(OUT_PATH),
        exist_ok=True,
    )

    # Load existing historical data, if available.
    existing = []

    if os.path.exists(OUT_PATH):

        with open(
            OUT_PATH,
            "r",
            encoding="utf-8",
        ) as f:

            existing_payload = json.load(f)

            existing = existing_payload.get(
                "series",
                [],
            )

    # Merge existing and new data.
    #
    # If the same date already exists,
    # the newly downloaded GME value replaces it.
    merged = {
        row["date"]: row["pun"]
        for row in existing
    }

    for row in daily:
        merged[row["date"]] = row["pun"]

    # Sort the complete dataset chronologically.
    series = [
        {
            "date": date,
            "pun": merged[date],
        }
        for date in sorted(merged)
    ]

    # Write the complete historical dataset.
    with open(
        OUT_PATH,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            {
                "source": "GME - PUN Index GME (MGP)",
                "series": series,
            },
            f,
            ensure_ascii=False,
        )

    print(
        f"Wrote {len(daily)} new PUN points."
    )

    print(
        f"Total historical PUN points: "
        f"{len(series)}"
    )

    print(
        f"Output: {OUT_PATH}"
    )


if __name__ == "__main__":
    main()