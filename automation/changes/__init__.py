"""Device changes: staged, then committed once. The execution log.

A change is STAGED (device, exact lines, rollback; checked against protected
lines; fingerprinted with the device's configuration as read then), then
COMMITTED once with that fingerprint and an idempotency key, refused before
anything is sent when the device's configuration changed since. Only a caller
with the execute scope does either. Whether a person approved the change is the
connected platform's business (AgenticOps's Control), never this stack's.

policy.py   the rules, pure functions: shape, fingerprint, once, the device's configuration
lines.py    protected lines, refused when a change is staged
store.py    the changes and every action on them, in SQLite (the execution log)

docs/how-to/run-changes.md
"""
