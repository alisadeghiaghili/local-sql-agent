# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
import random
import time
import requests
import concurrent.futures
from collections import Counter

BASE_URL = "http://localhost:8000"

QUESTIONS = [
    "پنج مشتری برتر",
    "ده عرضه کننده برتر",
    "ارزش معاملات سال 1403",
    "بیشترین کالای معامله شده",
    "کارگزار فروشنده برتر",
    "کارگزار خریدار برتر",
    "حجم عرضه به تفکیک ماه",
    "پنج کالای برتر بر اساس ارزش معاملات",
    "ارزش معاملات پتروشیمی در سال 1403",
    "بیشترین مشتری تالار سیمان",
]

TOTAL_REQUESTS = 1000
MAX_WORKERS = 20


def send_request():

    question = random.choice(
        QUESTIONS
    )

    payload = {
        "question": question
    }

    start = time.perf_counter()

    try:

        response = requests.post(
            f"{BASE_URL}/query",
            json=payload,
            timeout=120,
        )

        elapsed = (
            time.perf_counter()
            -
            start
        )

        return {
            "status": response.status_code,
            "time": elapsed,
            "question": question,
        }

    except Exception as ex:

        elapsed = (
            time.perf_counter()
            -
            start
        )

        return {
            "status": "ERROR",
            "time": elapsed,
            "question": question,
            "error": str(ex),
        }


def main():

    print("=" * 60)
    print("Auction NLQ Load Test")
    print("=" * 60)

    overall_start = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = [
            executor.submit(send_request)
            for _ in range(TOTAL_REQUESTS)
        ]

        results = [
            future.result()
            for future in futures
        ]

    overall_elapsed = (
        time.perf_counter()
        -
        overall_start
    )

    statuses = Counter(
        r["status"]
        for r in results
    )

    response_times = [
        r["time"]
        for r in results
    ]

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    print(
        f"Total Requests : {TOTAL_REQUESTS}"
    )

    print(
        f"Workers        : {MAX_WORKERS}"
    )

    print(
        f"Elapsed        : {overall_elapsed:.2f}s"
    )

    print()

    print(
        "Status Codes:"
    )

    for status, count in sorted(
        statuses.items(),
        key=lambda x: str(x[0])
    ):

        print(
            f"  {status}: {count}"
        )

    print()

    print(
        f"Min Response : "
        f"{min(response_times):.2f}s"
    )

    print(
        f"Avg Response : "
        f"{sum(response_times)/len(response_times):.2f}s"
    )

    print(
        f"Max Response : "
        f"{max(response_times):.2f}s"
    )

    print()

    failed = [
        r
        for r in results
        if r["status"] != 200
    ]

    if failed:

        print("=" * 60)
        print("FAILED REQUESTS")
        print("=" * 60)

        for item in failed[:20]:

            print(
                f"{item['status']} | "
                f"{item['question']}"
            )


if __name__ == "__main__":
    main()