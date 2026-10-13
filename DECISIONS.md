# DECISIONS.md

1. Indexes: unique employee code for safe concurrent creation; unique attendance (employee, date) for safe punch-in; department/employee and date/employee variants for filtered lists and analytics; employee/date/status for status-filtered analytics; an extra date-ascending variant so explain plans avoid sort-stage collection scans. I skipped a text index because every filter is an exact match.
2. Punch-in race: both requests compute the same IST date and call insert directly. MongoDB enforces the unique (employee, date) key, so one insert wins (201) and the other raises DuplicateKeyError, which I convert to 409.
3. Ties: leaderboard uses competition ranking, so equal late totals share one rank and the next rank skips (for example 1, 1, 3). Limit keeps rows with rank at most the limit, so ties at the cutoff return extra rows in employee-code order.
4. Headcount: department summary groups employees by department first, including staff with zero logs, then joins their monthly logs for presence and overtime math.
5. At 100x data I would pre-aggregate daily department rollups and cache trend windows, because per-request aggregation and moving averages would get too slow.

