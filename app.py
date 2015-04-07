from hashlib import sha256
import hmac
import json
import os
import os.path
import threading
import urlparse

from dropbox.client import DropboxClient, DropboxOAuth2Flow
from flask import abort, Flask, redirect, render_template, request, session, url_for
import redis
from yapf.yapflib.yapf_api import FormatCode
 
redis_url = os.environ['REDISTOGO_URL']
redis_client = redis.from_url(redis_url)

# App key and secret from the App console (dropbox.com/developers/apps)
APP_KEY = os.environ['APP_KEY']
APP_SECRET = os.environ['APP_SECRET']


app = Flask(__name__)
 
# A random secret used by Flask to encrypt session data cookies
app.secret_key = os.environ['FLASK_SECRET_KEY']

def get_url(route):
    '''Generate a proper URL, forcing HTTPS if not running locally'''
    host = urlparse.urlparse(request.url).hostname
    url = url_for(
        route,
        _external=True,
        _scheme='http' if host in ('127.0.0.1', 'localhost') else 'https'
    )

    return url

def get_flow():
    return DropboxOAuth2Flow(
        APP_KEY,
        APP_SECRET,
        get_url('oauth_callback'),
        session,
        'dropbox-csrf-token')

@app.route('/welcome')
def welcome():
    return render_template('welcome.html', redirect_url=get_url('oauth_callback'),
        webhook_url=get_url('webhook'), home_url=get_url('index'), app_key=APP_KEY)

@app.route('/oauth_callback')
def oauth_callback():
    '''Callback function for when the user returns from OAuth.'''

    access_token, uid, extras = get_flow().finish(request.args)
 
    # Extract and store the access token for this user
    redis_client.hset('tokens', uid, access_token)
    process_user(uid)

    return redirect(url_for('done'))

def credit(code):
    # for m in re.finditer('^__credits__ *= *(.+)$', code):
    #     m.start(), m.end(), m.group(1)
    code += "\n\n__credits__ = ['Dropbox API']\n"
    return code

def process_user(uid):
    '''Call /delta for the given user ID and process any changes.'''

    # OAuth token for the user
    token = redis_client.hget('tokens', uid)
    # /delta cursor for the user (None the first time)
    cursor = redis_client.hget('cursors', uid)
    client = DropboxClient(token)
    has_more = True
    while has_more:
        result = client.delta(cursor)
        
        for path, metadata in result['entries']:
            filename, fileext = os.path.splitext(path)
            # Ignore deleted files, folders, and non-python files
            if (metadata is None or metadata['is_dir'] or fileext != '.py'):
                continue

            with client.get_file(path) as fin:
                original_code = fin.read()
                try:
                    formatted_code = FormatCode(original_code)
                    suffix = "reformed"

                    # Only reform heretical code
                    if original_code != formatted_code:
                        formatted_code = credit(formatted_code)
                        client.put_file(filename + '-reformed.py', formatted_code, overwrite=True)

                except:
                    # code itself was somehow invalid.
                    with open('facepalm.py', 'rb') as facepalm:
                        client.put_file(filename + '-disappointed.py', facepalm, overwrite=True) 

        # Update cursor
        cursor = result['cursor']
        redis_client.hset('cursors', uid, cursor)

        # Repeat only if there's more to do
        has_more = result['has_more']

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login():
    return redirect(get_flow().start())

@app.route('/done')
def done(): 
    return render_template('done.html')

def validate_request():
    '''Validate that the request is properly signed by Dropbox.
       (If not, this is a spoofed webhook.)'''

    signature = request.headers.get('X-Dropbox-Signature')
    return signature == hmac.new(APP_SECRET, request.data, sha256).hexdigest()

@app.route('/webhook', methods=['GET'])
def challenge():
    '''Respond to the webhook challenge (GET request) by echoing back the challenge parameter.'''

    return request.args.get('challenge')

@app.route('/webhook', methods=['POST'])
def webhook():
    '''Receive a list of changed user IDs from Dropbox and process each.'''

    # Make sure this is a valid request from Dropbox
    if not validate_request(): abort(403)

    for uid in json.loads(request.data)['delta']['users']:
        # We need to respond quickly to the webhook request, so we do the
        # actual work in a separate thread. For more robustness, it's a
        # good idea to add the work to a reliable queue and process the queue
        # in a worker process.
        threading.Thread(target=process_user, args=(uid,)).start()
    return ''

if __name__=='__main__':
    app.run()
