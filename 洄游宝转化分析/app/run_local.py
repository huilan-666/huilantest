import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
from config.wsgi import application
from waitress import serve

if __name__=='__main__':
    serve(application,host='127.0.0.1',port=int(os.environ.get('HUILAN_PORT','8765')),threads=4)
