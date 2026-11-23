"""
Gunicorn configuration for the CryptoViz web process.

Referenced by the cryptoviz-web systemd unit:

    gunicorn --config deploy/gunicorn.conf.py cryptoviz.wsgi:app
"""

import os

# nginx talks to gunicorn over a unix socket rather than a TCP port, so the app
# is not reachable from outside the instance except through nginx.
bind = os.getenv("GUNICORN_BIND", "unix:/run/cryptoviz/gunicorn.sock")

# Fixed rather than CPU-based: t3 instances report 2 vCPUs at any memory size,
# and each worker holds its own copy of pandas and a SQLite page cache. With
# threads below, two workers are plenty for this workload.
workers = int(os.getenv("GUNICORN_WORKERS") or 2)

# Threads let a worker keep serving while another request waits on a query.
threads = int(os.getenv("GUNICORN_THREADS", 4))
worker_class = "gthread"

# Correlation over a 30-day window is the slowest endpoint; 60s is generous
# enough that a cold cache never trips the timeout.
timeout = int(os.getenv("GUNICORN_TIMEOUT", 60))
graceful_timeout = 30

# Keep connections open a little longer than nginx's default proxy behaviour.
keepalive = 5

# Recycle workers periodically so any slow leak in a long-running process is
# bounded; jitter avoids restarting them all at once.
max_requests = 1000
max_requests_jitter = 100

# Log to stdout/stderr; systemd routes both into the journal.
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("CRYPTOVIZ_LOG_LEVEL", "info").lower()
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'

# Name the processes so `ps` and `systemctl status` are readable.
proc_name = "cryptoviz-web"
