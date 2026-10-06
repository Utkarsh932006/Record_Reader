from __future__ import annotations

import pandas as pd

Interval = tuple[pd.Timestamp, pd.Timestamp]


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Merge overlapping [start, end) intervals."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda x: x[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        prev_s, prev_e = merged[-1]
        if start <= prev_e:
            merged[-1] = (prev_s, max(prev_e, end))
        else:
            merged.append((start, end))
    return merged


def interval_minutes(intervals: list[Interval]) -> float:
    return round(
        sum((end - start).total_seconds() / 60.0 for start, end in intervals),
        2,
    )


def clip_interval(
    start: pd.Timestamp,
    end: pd.Timestamp,
    window_start: pd.Timestamp | None,
    window_end: pd.Timestamp | None,
) -> Interval | None:
    if pd.isna(start) or pd.isna(end):
        return None
    s, e = start, end
    if window_start is not None:
        s = max(s, window_start)
    if window_end is not None:
        e = min(e, window_end)
    if e > s:
        return (s, e)
    return None


def intersect_intervals(left: list[Interval], right: list[Interval]) -> list[Interval]:
    """Return intersection of two merged interval lists."""
    a = merge_intervals(left)
    b = merge_intervals(right)
    out: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if e > s:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def overlap_minutes(
    a_start: pd.Timestamp,
    a_end: pd.Timestamp,
    b_start: pd.Timestamp,
    b_end: pd.Timestamp,
    slack: pd.Timedelta,
) -> float:
    """Return overlap minutes, or -1 if the windows do not touch even with slack."""
    if not (a_start - slack < b_end and a_end + slack > b_start):
        return -1.0
    raw_s = max(a_start, b_start)
    raw_e = min(a_end, b_end)
    if raw_e > raw_s:
        return (raw_e - raw_s).total_seconds() / 60.0
    return 0.0


def events_to_intervals(
    df: pd.DataFrame,
    start_col: str,
    end_col: str,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> list[Interval]:
    intervals: list[Interval] = []
    if df.empty:
        return intervals
    for start, end in zip(df[start_col], df[end_col], strict=True):
        clipped = clip_interval(start, end, window_start, window_end)
        if clipped:
            intervals.append(clipped)
    return merge_intervals(intervals)
