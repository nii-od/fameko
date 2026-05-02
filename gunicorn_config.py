import multiprocessing
import os

# Worker class for Flask-SocketIO with gevent WebSocket support
worker_class = 'geventwebsocket.gunicorn.workers.GeventWebSocketWorker'
# Use fewer workers to prevent memory issues on Railway (1GB RAM limit)
workers = 2

# WebSocket support
worker_connections = 1000

# Timeout settings
timeout = 120
keepalive = 2

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'

# Process naming
proc_name = 'fameko'

# Bind address - use PORT env var (Railway/Render provide this), default to 10000
port = os.environ.get('PORT', '10000')
bind = f'0.0.0.0:{port}'
