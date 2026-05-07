from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import os
import requests
from collections import Counter

app = Flask(__name__, static_folder='static')
CORS(app)

GHL_TOKEN = os.environ.get('GHL_API_TOKEN', '')
GHL_LOCATION = os.environ.get('GHL_LOCATION_ID', '')
MONDAY_TOKEN = os.environ.get('MONDAY_API_TOKEN', '')

GHL_BASE = 'https://services.leadconnectorhq.com'
MONDAY_BASE = 'https://api.monday.com/v2'


def ghl_get(endpoint, params=None):
    headers = {
        'Authorization': f'Bearer {GHL_TOKEN}',
        'Version': '2021-07-28',
        'Content-Type': 'application/json'
    }
    resp = requests.get(f'{GHL_BASE}{endpoint}', headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def monday_query(query):
    headers = {
        'Authorization': MONDAY_TOKEN,
        'Content-Type': 'application/json'
    }
    resp = requests.post(MONDAY_BASE, headers=headers, json={'query': query})
    resp.raise_for_status()
    return resp.json()


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/data')
def get_data():
    # 1. Fetch 50 GHL contacts
    contacts_resp = ghl_get('/contacts/', {
        'locationId': GHL_LOCATION,
        'limit': 50,
        'sortBy': 'date_added',
        'order': 'desc'
    })
    contacts = contacts_resp.get('contacts', [])

    # Get pipeline stage map
    pipelines_resp = ghl_get('/opportunities/pipelines', {'locationId': GHL_LOCATION})
    stage_map = {}
    for p in pipelines_resp.get('pipelines', []):
        for s in p.get('stages', []):
            stage_map[s['id']] = s['name']

    # Get opportunities to map contacts to stages
    opps_resp = ghl_get('/opportunities/search', {
        'location_id': GHL_LOCATION,
        'limit': 100,
        'status': 'open'
    })
    contact_stages = {}
    for opp in opps_resp.get('opportunities', []):
        cid = opp.get('contactId')
        if cid:
            contact_stages[cid] = stage_map.get(opp.get('pipelineStageId', ''), 'Unknown')

    # Build GHL rows
    ghl_rows = []
    ghl_emails = {}
    for c in contacts:
        email = c.get('email') or ''
        name = (c.get('contactName') or
                f"{c.get('firstName', '') or ''} {c.get('lastName', '') or ''}".strip() or
                c.get('phone', 'Unknown'))
        stage = contact_stages.get(c['id'], 'No Pipeline')
        row = {'name': name, 'email': email, 'stage': stage}
        ghl_rows.append(row)
        if email:
            ghl_emails[email.lower()] = row

    # 2. Fetch Monday.com items
    query = '''{ boards(ids: [1944309746]) { items_page(limit: 50,
        query_params: {order_by: [{column_id: "__last_updated__", direction: desc}]})
        { items { id name column_values { id text column { title } } } } } }'''
    monday_resp = monday_query(query)
    items = monday_resp['data']['boards'][0]['items_page']['items']

    monday_rows = []
    for item in items:
        row = {'name': item['name'], 'status': '', 'owner': '', 'email': ''}
        for cv in item['column_values']:
            title = cv.get('column', {}).get('title', '')
            val = cv.get('text', '') or ''
            if title == 'Payment Status':
                row['status'] = val
            elif title == 'Deal Owner':
                row['owner'] = val
            elif 'Email' in title and '@' in val:
                row['email'] = val.split('/')[0].strip()
            elif title == 'Contact Name' and val:
                row['contact'] = val
        monday_rows.append(row)

    # 3. Match by email
    matched = []
    for m in monday_rows:
        email = (m.get('email') or '').lower().strip()
        ghl_match = ghl_emails.get(email)
        if ghl_match:
            matched.append({
                'business': m['name'],
                'contact': m.get('contact', ''),
                'email': email,
                'ghl_stage': ghl_match['stage'],
                'monday_status': m['status'],
                'owner': m['owner']
            })

    # 4. Stage counts
    stage_counts = dict(Counter(r['stage'] for r in ghl_rows).most_common())

    return jsonify({
        'ghl_rows': ghl_rows,
        'monday_rows': monday_rows,
        'matched': matched,
        'stage_counts': stage_counts,
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
