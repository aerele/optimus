"""Shared, side-effect-free lease and delivery rules for durable background work."""

QUEUE_SECONDS = 1800
DISPATCH_INTERVAL = 30


def lease_alive(until, now):
	return (until or 0) > now


def owner_matches(actual, expected, until, now):
	return bool(expected and actual == expected and lease_alive(until, now))


def dispatch_due(until, previous, now, interval):
	return lease_alive(until, now) and (not previous or previous + interval <= now)
