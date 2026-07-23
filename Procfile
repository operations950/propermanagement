web: python manage.py migrate --noinput && python manage.py bootstrap_admin && python manage.py collectstatic --noinput && gunicorn proptasks.wsgi --bind 0.0.0.0:$PORT --log-file -
