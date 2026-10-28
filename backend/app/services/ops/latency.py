"""Route latency from the merged per-minute histograms (ADR §11, D23, OPS-04b).

Each process flushes one row per minute, method and route template with a
log-scale duration histogram (``ws_ops_route_stats``). A window query sums
those rows per route and merges the buckets, because a percentile across
processes cannot be recovered from per-process percentiles. The estimate is the
upper bound of the bucket where the cumulative count crosses the quantile: it
is therefore never below the true value, and one bucket wide. The overflow
bucket (over ``stats.BUCKETS_MS[-1]``) has no upper bound, so a quantile that
lands there is ``None`` and the route carries ``overflow``.

Routes are ordered by estimated total time, the count weighted by each bucket's
midpoint, so a frequently hit route sorts above a rare slow one. The overflow
bucket uses the last bound as a conservative floor for that estimate.
"""

from __future__ import annotations

from typing import Any

from app.services.ops.stats import BUCKETS_MS
from app.services.postgres_database import database

DEFAULT_WINDOW_MINUTES = 60
MIN_WINDOW_MINUTES = 1
MAX_WINDOW_MINUTES = 7 * 24 * 60
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

#: Quantiles reported for every route and for the overall totals.
QUANTILES: tuple[float, ...] = (0.50, 0.95, 0.99)


def _clamp(value: int, low: int, high: int) -> int:
    return min(max(int(value), low), high)


def _percentile(buckets: list[int] | tuple[int, ...], quantile: float) -> tuple[float | None, bool]:
    """``(upper bound, overflowed)`` for ``quantile`` over a merged histogram."""

    total = sum(int(count) for count in buckets)
    if total <= 0:
        return None, False
    threshold = float(quantile) * total
    cumulative = 0
    for index, count in enumerate(buckets):
        cumulative += int(count)
        if cumulative >= threshold:
            if index >= len(BUCKETS_MS):
                return None, True
            return float(BUCKETS_MS[index]), False
    return None, False


def _estimated_total_ms(buckets: list[int] | tuple[int, ...]) -> float:
    """The histogram's total time, counting each bucket at its midpoint.

    The overflow bucket has no midpoint; its lower bound is used, so the
    estimate is a floor rather than an unbounded guess.
    """

    lower = 0
    total = 0.0
    for index, count in enumerate(buckets):
        count = int(count)
        if index < len(BUCKETS_MS):
            upper = BUCKETS_MS[index]
            midpoint = (lower + upper) / 2
            lower = upper
        else:
            midpoint = float(BUCKETS_MS[-1])
        total += count * midpoint
    return total


def _numbers(buckets: list[int], count: int, status_4xx: int, status_5xx: int) -> dict[str, Any]:
    """The counts, rates and percentiles shared by a route and the totals."""

    p50, p50_overflow = _percentile(buckets, QUANTILES[0])
    p95, p95_overflow = _percentile(buckets, QUANTILES[1])
    p99, p99_overflow = _percentile(buckets, QUANTILES[2])
    return {
        "count": count,
        "status4xx": status_4xx,
        "status5xx": status_5xx,
        "rate4xx": (status_4xx / count) if count else 0.0,
        "rate5xx": (status_5xx / count) if count else 0.0,
        "p50Ms": p50,
        "p95Ms": p95,
        "p99Ms": p99,
        "overflow": p50_overflow or p95_overflow or p99_overflow,
        "estimatedTotalMs": round(_estimated_total_ms(buckets), 3),
    }


def _merge(rows: list[dict[str, Any]], total_buckets: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Per-route numbers plus one merged histogram for the overall totals."""

    overall = [0] * total_buckets
    count = status_4xx = status_5xx = 0
    routes: list[dict[str, Any]] = []
    for row in rows:
        buckets = [int(value) for value in (row["buckets"] or [])]
        if len(buckets) != total_buckets:
            continue
        row_count = int(row["count"] or 0)
        row_4xx = int(row["status_4xx"] or 0)
        row_5xx = int(row["status_5xx"] or 0)
        overall = [a + b for a, b in zip(overall, buckets)]
        count += row_count
        status_4xx += row_4xx
        status_5xx += row_5xx
        routes.append(
            {
                "method": str(row["method"]),
                "route": str(row["route"]),
                **_numbers(buckets, row_count, row_4xx, row_5xx),
            }
        )

    routes.sort(key=lambda item: (-item["estimatedTotalMs"], item["route"], item["method"]))
    totals = {**_numbers(overall, count, status_4xx, status_5xx), "routeCount": len(routes)}
    return totals, routes


def route_latency(
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    route_prefix: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Merge the window's route histograms and return the slowest routes and totals.

    ``route_prefix`` filters on the route template's literal prefix, so a
    caller can ask about ``/api/projects/`` without a LIKE pattern.
    """

    window = _clamp(window_minutes, MIN_WINDOW_MINUTES, MAX_WINDOW_MINUTES)
    limit = _clamp(limit, 1, MAX_LIMIT)
    prefix = (route_prefix or "").strip()
    filters = [
        "minute >= NOW() - make_interval(mins => %s)",
        "minute <= NOW()",
    ]
    params: list[Any] = [window]
    if prefix:
        filters.append("LEFT(route, %s) = %s")
        params.extend([len(prefix), prefix])
    where = " AND ".join(filters)

    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        rows = conn.execute(
            f"""
            WITH filtered AS (
                SELECT method, route, count, status_4xx, status_5xx, buckets
                FROM ws_ops_route_stats
                WHERE {where}
            ),
            expanded AS (
                SELECT method, route, ord, bucket
                FROM filtered, unnest(filtered.buckets) WITH ORDINALITY AS u(bucket, ord)
            ),
            hist AS (
                SELECT method, route, ord, SUM(bucket)::bigint AS bucket
                FROM expanded
                GROUP BY method, route, ord
            )
            SELECT f.method, f.route,
                   SUM(f.count)::bigint AS count,
                   SUM(f.status_4xx)::bigint AS status_4xx,
                   SUM(f.status_5xx)::bigint AS status_5xx,
                   (
                       SELECT array_agg(h.bucket ORDER BY h.ord)
                       FROM hist h
                       WHERE h.method = f.method AND h.route = f.route
                   ) AS buckets
            FROM filtered f
            GROUP BY f.method, f.route
            """,
            tuple(params),
        ).fetchall()

    totals, routes = _merge(rows, len(BUCKETS_MS) + 1)
    return {
        "windowMinutes": window,
        "totals": totals,
        "routes": routes[:limit],
    }


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_WINDOW_MINUTES",
    "MAX_LIMIT",
    "MAX_WINDOW_MINUTES",
    "MIN_WINDOW_MINUTES",
    "QUANTILES",
    "route_latency",
]
