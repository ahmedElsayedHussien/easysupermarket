import os
from waitress import serve
from config.wsgi import application

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"Starting Waitress server on 0.0.0.0:{port}...")
    serve(application, host='0.0.0.0', port=port)
