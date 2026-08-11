# Background jobs, scheduler, and monitoring pattern

Apply to queues, workers, scheduled commands, cron, heartbeats, retries, alerts, or asynchronous state.

## Coverage prompts

- Dispatch the exact job or run the scheduled command with safe input.
- Verify queued/running/succeeded/failed state and the user-visible result.
- Check retry count/backoff, idempotency, duplicate dispatch, timeout, and dead-letter behavior.
- Confirm transaction boundaries prevent jobs from observing uncommitted or missing state.
- Verify scheduler expression, timezone, overlap protection, and missed-run recovery.
- Check heartbeat/monitor freshness, correlation IDs, logs, alert threshold, and recovery visibility.
- Stop or clean up test jobs, schedules, records, and notifications.
- Record unavailable worker/scheduler/provider infrastructure as a structured limit.

Use job class, queue, schedule, command, monitor, and resulting-data anchors. Never equate dispatch with successful completion.
