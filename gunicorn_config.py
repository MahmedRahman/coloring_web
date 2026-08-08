# Gunicorn production config
bind = "0.0.0.0:5000"
workers = 2
threads = 4
timeout = 360
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
