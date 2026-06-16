import time
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

from llm.sql_agent import SQLAgent


SYSTEM_PROMPT = """
You are a SQL generator.
"""

QUESTIONS = [
    "Top 10 customers",
    "Top 10 suppliers",
    "Monthly turnover",
    "Total contract value",
    "Top products",
]


def run_single(agent, question):

    started = time.perf_counter()

    try:

        df, result = agent.run(
            question=question,
            system_prompt=SYSTEM_PROMPT,
        )

        elapsed = (
            time.perf_counter()
            - started
        )

        return {
            "success": True,
            "question": question,
            "latency": elapsed,
            "attempt": result.attempt,
            "rows": len(df),
        }

    except Exception as exc:

        elapsed = (
            time.perf_counter()
            - started
        )

        return {
            "success": False,
            "question": question,
            "latency": elapsed,
            "attempt": 0,
            "rows": 0,
            "error": str(exc),
        }


def percentile(values, p):

    values = sorted(values)

    k = (len(values) - 1) * p

    f = int(k)

    c = min(
        f + 1,
        len(values) - 1
    )

    if f == c:
        return values[f]

    return (
        values[f]
        + (
            values[c]
            - values[f]
        )
        * (k - f)
    )


def run_stress_test():

    agent = SQLAgent()

    requests = []

    for _ in range(100):

        for q in QUESTIONS:

            requests.append(q)

    results = []

    started = time.perf_counter()

    with ThreadPoolExecutor(
        max_workers=10
    ) as pool:

        futures = [
            pool.submit(
                run_single,
                agent,
                question
            )
            for question in requests
        ]

        for future in as_completed(futures):

            results.append(
                future.result()
            )

    total_elapsed = (
        time.perf_counter()
        - started
    )

    successes = [
        r
        for r in results
        if r["success"]
    ]

    failures = [
        r
        for r in results
        if not r["success"]
    ]

    latencies = [
        r["latency"]
        for r in results
    ]

    corrections = sum(
        1
        for r in successes
        if r["attempt"] > 1
    )

    print()
    print("=" * 60)
    print("SQL AGENT STRESS TEST")
    print("=" * 60)

    print(
        f"Requests          : {len(results)}"
    )

    print(
        f"Success           : {len(successes)}"
    )

    print(
        f"Failed            : {len(failures)}"
    )

    print(
        f"Correction Count  : {corrections}"
    )

    print()

    print(
        f"Total Duration    : {total_elapsed:.2f}s"
    )

    print(
        f"Avg Latency       : "
        f"{statistics.mean(latencies):.2f}s"
    )

    print(
        f"P95 Latency       : "
        f"{percentile(latencies, 0.95):.2f}s"
    )

    print(
        f"P99 Latency       : "
        f"{percentile(latencies, 0.99):.2f}s"
    )

    print("=" * 60)


if __name__ == "__main__":
    run_stress_test()