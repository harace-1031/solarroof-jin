import os
bind='0.0.0.0:'+os.environ.get('PORT','10000')
workers=1
worker_class='gthread'
threads=10
timeout=180
accesslog=None
errorlog='-'
