from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import os
import json
import requests
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import Counter

app = Flask(__name__, static_folder='static')
CORS(app)

GHL_TOKEN = os.environ.get('GHL_API_TOKEN', '')
GHL_LOCATION = os.environ.get('GHL_LOCATION_ID', '')
MONDAY_TOKEN = os.environ.get('MONDAY_API_TOKEN', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'usmd-calls-2024')
LOCAL_TZ = ZoneInfo(os.environ.get('DASHBOARD_TIMEZONE', 'America/Los_Angeles'))

GHL_BASE = 'https://services.leadconnectorhq.com'
MONDAY_BASE = 'https://api.monday.com/v2'

CALLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'calls_data.json')

cache = {}
CACHE_TTL = 300


def cached(key, fetcher):
    now = time.time()
    if key in cache and now - cache[key]['ts'] < CACHE_TTL:
        return cache[key]['data']
    data = fetcher()
    cache[key] = {'data': data, 'ts': now}
    return data


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


def parse_date(d):
    if not d:
        return None
    if isinstance(d, (int, float)):
        return datetime.fromtimestamp(d / 1000, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(d.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def to_local_month(dt):
    """Convert a GHL UTC timestamp to local timezone and return YYYY-MM."""
    if dt is None:
        return None
    local_dt = dt.astimezone(LOCAL_TZ)
    return local_dt.strftime('%Y-%m')


def date_to_month(d):
    """Extract YYYY-MM from a Monday.com date-only string (no timezone conversion)."""
    if not d or len(d) < 7:
        return None
    return d[:7]


def in_range(dt, start, end):
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if start and dt < start:
        return False
    if end and dt > end:
        return False
    return True


def get_date_range():
    """UTC boundaries — use for Monday.com date-only fields."""
    start_str = request.args.get('start')
    end_str = request.args.get('end')
    start = datetime.fromisoformat(start_str).replace(tzinfo=timezone.utc) if start_str else None
    end = datetime.fromisoformat(end_str).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc) if end_str else None
    return start, end


def get_date_range_local():
    """Pacific time boundaries — use for GHL UTC timestamps."""
    start_str = request.args.get('start')
    end_str = request.args.get('end')
    start = datetime.fromisoformat(start_str).replace(tzinfo=LOCAL_TZ) if start_str else None
    end = datetime.fromisoformat(end_str).replace(hour=23, minute=59, second=59, tzinfo=LOCAL_TZ) if end_str else None
    return start, end


def load_calls():
    if os.path.exists(CALLS_FILE):
        with open(CALLS_FILE, 'r') as f:
            return json.load(f)
    return []


def save_calls(calls):
    with open(CALLS_FILE, 'w') as f:
        json.dump(calls, f)


def fetch_all_contacts():
    def _fetch():
        all_contacts = []
        params = {'locationId': GHL_LOCATION, 'limit': 100, 'sortBy': 'date_added', 'order': 'desc'}
        for _ in range(50):
            resp = ghl_get('/contacts/', params)
            contacts = resp.get('contacts', [])
            if not contacts:
                break
            all_contacts.extend(contacts)
            meta = resp.get('meta', {})
            start_after = meta.get('startAfter')
            start_after_id = meta.get('startAfterId')
            if not start_after or not start_after_id:
                break
            params['startAfter'] = start_after
            params['startAfterId'] = start_after_id
        return all_contacts
    return cached('contacts', _fetch)


def fetch_opportunities(status):
    def _fetch():
        all_opps = []
        params = {'location_id': GHL_LOCATION, 'limit': 100, 'status': status}
        for _ in range(10):
            resp = ghl_get('/opportunities/search', params)
            opps = resp.get('opportunities', [])
            if not opps:
                break
            all_opps.extend(opps)
            meta = resp.get('meta', {})
            start_after = meta.get('startAfter')
            start_after_id = meta.get('startAfterId')
            if not start_after or not start_after_id:
                break
            params['startAfter'] = start_after
            params['startAfterId'] = start_after_id
        return all_opps
    return cached(f'opps_{status}', _fetch)


def fetch_conversations():
    def _fetch():
        all_convos = []
        params = {'locationId': GHL_LOCATION, 'limit': 100, 'type': 'TYPE_PHONE'}
        for _ in range(20):
            resp = ghl_get('/conversations/search', params)
            convos = resp.get('conversations', [])
            if not convos:
                break
            all_convos.extend(convos)
            if len(convos) < 100:
                break
            last = convos[-1]
            params['startAfterDate'] = last.get('dateAdded')
        return all_convos
    return cached('conversations', _fetch)


def fetch_sales_data():
    def _fetch():
        query = '''{ boards(ids: [1944309746]) {
            groups(ids: ["new_group_mkkazbjx"]) {
            items_page(limit: 500) { items { id name
            column_values(ids: ["numbers_Mjivm65q", "date4", "date_Mjiv0T8Z", "status",
            "text_MjivuCB8", "text_MjivTjDy"]) { id text column { title } } } } } } }'''
        resp = monday_query(query)
        records = []
        for group in resp['data']['boards'][0]['groups']:
            for item in group['items_page']['items']:
                fee = 0
                first_payment = ''
                last_payment = ''
                status = ''
                contact = ''
                email = ''
                for cv in item['column_values']:
                    cid = cv['id']
                    text = cv.get('text', '') or ''
                    if cid == 'numbers_Mjivm65q':
                        try:
                            fee = float(text) if text else 0
                        except ValueError:
                            fee = 0
                    elif cid == 'date4':
                        first_payment = text
                    elif cid == 'date_Mjiv0T8Z':
                        last_payment = text
                    elif cid == 'status':
                        status = text
                    elif cid == 'text_MjivuCB8':
                        contact = text
                    elif cid == 'text_MjivTjDy':
                        email = text
                records.append({
                    'name': item['name'],
                    'fee': fee,
                    'first_payment': first_payment,
                    'last_payment': last_payment,
                    'date': first_payment,
                    'status': status,
                    'contact': contact,
                    'email': email
                })
        return records
    return cached('sales', _fetch)


def fetch_payment_data():
    def _fetch():
        query = '''{ boards(ids: [1944525313]) {
            groups(ids: ["1733660267_book1_usmd_dec_new_Mjj1XfHC"]) {
            items_page(limit: 500) { items { id name
            column_values(ids: ["status_Mjj1A9wh", "date_mks817p7", "date_mkmtbt5h",
            "numeric_mkty8mvs", "text_mktzxv3k"]) { id text column { title } } } } } } }'''
        resp = monday_query(query)
        items = resp['data']['boards'][0]['groups'][0]['items_page']['items']

        sub_type_query = '''{ boards(ids: [1944309746]) {
            groups(ids: ["1733734937_book1_usmd_dec_new_Mjj2w4It"]) {
            items_page(limit: 500) { items { name
            column_values(ids: ["color_mm1gq51r"]) { text } } } } } }'''
        sub_resp = monday_query(sub_type_query)
        sub_items = sub_resp['data']['boards'][0]['groups'][0]['items_page']['items']
        sub_type_map = {}
        for si in sub_items:
            val = si['column_values'][0]['text'] if si['column_values'] else ''
            if val:
                sub_type_map[si['name'].strip().lower()] = val

        records = []
        for item in items:
            status = ''
            start_date = ''
            end_date = ''
            due_date = ''
            ar = ''
            for cv in item['column_values']:
                cid = cv['id']
                text = cv.get('text', '') or ''
                if cid == 'status_Mjj1A9wh':
                    status = text
                elif cid == 'date_mks817p7':
                    start_date = text
                elif cid == 'date_mkmtbt5h':
                    end_date = text
                elif cid == 'numeric_mkty8mvs':
                    due_date = text
                elif cid == 'text_mktzxv3k':
                    ar = text
            sub_type = sub_type_map.get(item['name'].strip().lower(), '')
            records.append({
                'name': item['name'],
                'status': status,
                'start_date': start_date,
                'end_date': end_date,
                'due_date': due_date,
                'ar': ar,
                'subscription_type': sub_type
            })
        return records
    return cached('payments', _fetch)


def fetch_churn_data():
    def _fetch():
        query = '''{ boards(ids: [1944525313]) { groups(ids: ["group_title"]) {
            items_page(limit: 500) { cursor items { id name
            column_values(ids: ["date_mkmtbt5h", "date_mks817p7", "status_Mjj1A9wh"])
            { id text } } } } } }'''
        resp = monday_query(query)
        items = resp['data']['boards'][0]['groups'][0]['items_page']['items']
        records = []
        for item in items:
            end_date = ''
            start_date = ''
            status = ''
            for cv in item['column_values']:
                if cv['id'] == 'date_mkmtbt5h':
                    end_date = cv.get('text', '')
                elif cv['id'] == 'date_mks817p7':
                    start_date = cv.get('text', '')
                elif cv['id'] == 'status_Mjj1A9wh':
                    status = cv.get('text', '')
            records.append({
                'name': item['name'],
                'end_date': end_date,
                'start_date': start_date,
                'status': status or 'Cancelled/Inactive'
            })
        return records
    return cached('churn', _fetch)


def fetch_pipeline_data():
    def _fetch():
        resp = ghl_get('/opportunities/pipelines', {'locationId': GHL_LOCATION})
        pipelines = resp.get('pipelines', [])
        stage_map = {}
        for p in pipelines:
            for s in p.get('stages', []):
                stage_map[s['id']] = s['name']
        opps = fetch_opportunities('open')
        stage_counts = Counter()
        for opp in opps:
            stage_name = stage_map.get(opp.get('pipelineStageId', ''), 'Unknown')
            stage_counts[stage_name] += 1
        return dict(stage_counts.most_common(15))
    return cached('pipeline', _fetch)


@app.route('/api/webhook/calls', methods=['POST'])
def webhook_calls():
    token = request.headers.get('X-Webhook-Secret') or request.args.get('secret')
    if token != WEBHOOK_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json or {}

    call_record = {
        'id': data.get('id', str(int(time.time() * 1000))),
        'contactId': data.get('contactId', ''),
        'contactName': data.get('contactName', data.get('contact_name', '')),
        'phone': data.get('phone', data.get('to', '')),
        'direction': data.get('direction', data.get('callDirection', 'outbound')),
        'duration': data.get('duration', data.get('callDuration', 0)),
        'status': data.get('status', data.get('callStatus', 'completed')),
        'timestamp': data.get('timestamp', data.get('dateAdded', datetime.now(timezone.utc).isoformat())),
        'userId': data.get('userId', data.get('assigned_to', '')),
        'source': 'webhook'
    }

    calls = load_calls()
    if not any(c.get('id') == call_record['id'] for c in calls):
        calls.append(call_record)
        save_calls(calls)

    return jsonify({'status': 'ok', 'recorded': call_record['id']}), 200


@app.route('/api/webhook/calls/bulk', methods=['POST'])
def webhook_calls_bulk():
    token = request.headers.get('X-Webhook-Secret') or request.args.get('secret')
    if token != WEBHOOK_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json or {}
    records = data.get('calls', [])

    calls = load_calls()
    existing_ids = {c.get('id') for c in calls}
    added = 0

    for record in records:
        call_record = {
            'id': record.get('id', str(int(time.time() * 1000) + added)),
            'contactId': record.get('contactId', ''),
            'contactName': record.get('contactName', record.get('contact_name', '')),
            'phone': record.get('phone', record.get('to', '')),
            'direction': record.get('direction', record.get('callDirection', 'outbound')),
            'duration': record.get('duration', record.get('callDuration', 0)),
            'status': record.get('status', record.get('callStatus', 'completed')),
            'timestamp': record.get('timestamp', record.get('dateAdded', datetime.now(timezone.utc).isoformat())),
            'userId': record.get('userId', record.get('assigned_to', '')),
            'source': 'webhook'
        }
        if call_record['id'] not in existing_ids:
            calls.append(call_record)
            existing_ids.add(call_record['id'])
            added += 1

    save_calls(calls)
    return jsonify({'status': 'ok', 'added': added, 'total': len(calls)}), 200


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/kpis')
def get_kpis():
    start, end = get_date_range()
    start_local, end_local = get_date_range_local()

    contacts = fetch_all_contacts()
    leads = [c for c in contacts if in_range(parse_date(c.get('dateAdded')), start_local, end_local)]

    sales_data = fetch_sales_data()
    sales = [s for s in sales_data if in_range(parse_date(s.get('first_payment')), start, end)]
    total_revenue = sum(s['fee'] for s in sales)

    churn_data = fetch_churn_data()
    churn = [c for c in churn_data if in_range(parse_date(c.get('end_date')), start, end)]

    webhook_calls = load_calls()
    calls_source = 'webhook'
    if webhook_calls:
        calls = [c for c in webhook_calls if in_range(parse_date(c.get('timestamp')), start_local, end_local)]
        outbound_calls = [c for c in calls if c.get('direction', '').lower() in ('outbound', 'outgoing')]
    else:
        convos = fetch_conversations()
        calls = [c for c in convos if in_range(parse_date(c.get('dateAdded')), start_local, end_local)]
        outbound_calls = calls
        calls_source = 'conversations_api'

    payment_data = fetch_payment_data()
    active_clients = [p for p in payment_data if p.get('status') == 'Active']

    sales_count = len(sales)
    avg_per_sale = total_revenue / sales_count if sales_count > 0 else 0

    return jsonify({
        'leads': len(leads),
        'sales': sales_count,
        'churn': len(churn),
        'calls': len(calls),
        'outbound_calls': len(outbound_calls),
        'revenue': round(total_revenue, 2),
        'avg_per_sale': round(avg_per_sale, 2),
        'active_clients': len(active_clients),
        'total_contacts': len(contacts),
        'calls_source': calls_source
    })


@app.route('/api/leads')
def get_leads():
    start_local, end_local = get_date_range_local()
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))

    contacts = fetch_all_contacts()
    filtered = [c for c in contacts if in_range(parse_date(c.get('dateAdded')), start_local, end_local)]
    filtered.sort(key=lambda c: c.get('dateAdded', ''), reverse=True)

    total = len(filtered)
    start_idx = (page - 1) * limit
    page_data = filtered[start_idx:start_idx + limit]

    rows = []
    for c in page_data:
        name = (c.get('contactName') or
                f"{c.get('firstName', '') or ''} {c.get('lastName', '') or ''}".strip() or
                c.get('phone', 'Unknown'))
        rows.append({
            'name': name,
            'email': c.get('email', ''),
            'phone': c.get('phone', ''),
            'date': c.get('dateAdded', ''),
            'source': c.get('source', ''),
            'tags': c.get('tags', [])
        })

    return jsonify({'rows': rows, 'total': total, 'page': page, 'pages': (total + limit - 1) // limit})


@app.route('/api/sales')
def get_sales():
    start, end = get_date_range()
    sales_data = fetch_sales_data()
    filtered = [s for s in sales_data if in_range(parse_date(s.get('first_payment')), start, end)]
    filtered.sort(key=lambda s: s.get('first_payment', ''), reverse=True)

    rows = []
    for s in filtered:
        rows.append({
            'name': s.get('name', ''),
            'contact': s.get('contact', ''),
            'email': s.get('email', ''),
            'fee': s.get('fee', 0),
            'status': s.get('status', ''),
            'first_payment': s.get('first_payment', ''),
            'last_payment': s.get('last_payment', ''),
            'date': s.get('first_payment', '')
        })

    return jsonify({'rows': rows, 'total': len(rows)})


@app.route('/api/churn')
def get_churn():
    start, end = get_date_range()
    churn_data = fetch_churn_data()
    filtered = [c for c in churn_data if in_range(parse_date(c.get('end_date')), start, end)]
    filtered.sort(key=lambda c: c.get('end_date', ''), reverse=True)

    rows = []
    for c in filtered:
        rows.append({
            'name': c.get('name', ''),
            'status': c.get('status', ''),
            'start_date': c.get('start_date', ''),
            'date': c.get('end_date', '')
        })

    return jsonify({'rows': rows, 'total': len(rows)})


@app.route('/api/calls')
def get_calls():
    start_local, end_local = get_date_range_local()
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    direction_filter = request.args.get('direction')

    webhook_calls = load_calls()

    if webhook_calls:
        filtered = []
        for c in webhook_calls:
            dt = parse_date(c.get('timestamp'))
            if not in_range(dt, start_local, end_local):
                continue
            if direction_filter and c.get('direction', '').lower() != direction_filter.lower():
                continue
            filtered.append(c)

        filtered.sort(key=lambda c: c.get('timestamp', ''), reverse=True)

        total = len(filtered)
        start_idx = (page - 1) * limit
        page_data = filtered[start_idx:start_idx + limit]

        rows = []
        for c in page_data:
            rows.append({
                'name': c.get('contactName', ''),
                'phone': c.get('phone', ''),
                'direction': c.get('direction', ''),
                'duration': c.get('duration', 0),
                'status': c.get('status', ''),
                'date': c.get('timestamp', ''),
                'source': 'webhook'
            })

        return jsonify({
            'rows': rows,
            'total': total,
            'page': page,
            'pages': (total + limit - 1) // limit,
            'data_source': 'webhook',
            'note': 'Call data from GHL workflow webhook'
        })

    convos = fetch_conversations()
    filtered = [c for c in convos if in_range(parse_date(c.get('dateAdded')), start_local, end_local)]
    filtered.sort(key=lambda c: c.get('dateAdded', 0), reverse=True)

    total = len(filtered)
    start_idx = (page - 1) * limit
    page_data = filtered[start_idx:start_idx + limit]

    rows = []
    for c in page_data:
        rows.append({
            'name': c.get('fullName', ''),
            'phone': c.get('phone', ''),
            'email': c.get('email', ''),
            'date': c.get('dateAdded', ''),
            'lastMessage': c.get('lastMessageBody', '')[:100],
            'direction': c.get('lastMessageDirection', '')
        })

    return jsonify({
        'rows': rows,
        'total': total,
        'page': page,
        'pages': (total + limit - 1) // limit,
        'data_source': 'conversations_api',
        'note': 'Fallback: showing conversation threads (not individual calls). Connect GHL Workflow to get actual call data.'
    })


@app.route('/api/calls/stats')
def get_calls_stats():
    start_local, end_local = get_date_range_local()
    webhook_calls = load_calls()

    if not webhook_calls:
        convos = fetch_conversations()
        calls = [c for c in convos if in_range(parse_date(c.get('dateAdded')), start_local, end_local)]
        return jsonify({
            'total': len(calls),
            'data_source': 'conversations_api',
            'note': 'No webhook data available. Showing conversation thread count.'
        })

    filtered = [c for c in webhook_calls if in_range(parse_date(c.get('timestamp')), start_local, end_local)]

    direction_counts = Counter(c.get('direction', 'unknown').lower() for c in filtered)
    status_counts = Counter(c.get('status', 'unknown').lower() for c in filtered)

    monthly = {}
    for c in filtered:
        dt = parse_date(c.get('timestamp'))
        key = to_local_month(dt)
        if key:
            monthly[key] = monthly.get(key, 0) + 1

    sorted_months = sorted(monthly.items())

    return jsonify({
        'total': len(filtered),
        'by_direction': dict(direction_counts),
        'by_status': dict(status_counts),
        'monthly': [{'month': m, 'count': v} for m, v in sorted_months],
        'data_source': 'webhook'
    })


@app.route('/api/revenue')
def get_revenue():
    start, end = get_date_range()
    sales_data = fetch_sales_data()
    filtered = [s for s in sales_data if in_range(parse_date(s.get('first_payment')), start, end)]

    monthly = {}
    for s in filtered:
        key = date_to_month(s.get('first_payment'))
        if key:
            monthly[key] = monthly.get(key, 0) + s['fee']

    sorted_months = sorted(monthly.items())

    return jsonify({
        'rows': [{'name': s['name'], 'fee': s['fee'], 'date': s['first_payment'], 'status': s['status']} for s in filtered],
        'monthly': [{'month': m, 'revenue': round(v, 2)} for m, v in sorted_months],
        'total': round(sum(s['fee'] for s in filtered), 2)
    })


@app.route('/api/payments')
def get_payments():
    payment_data = fetch_payment_data()
    status_filter = request.args.get('status')
    if status_filter:
        payment_data = [p for p in payment_data if p.get('status', '').lower() == status_filter.lower()]

    rows = []
    for p in payment_data:
        rows.append({
            'name': p.get('name', ''),
            'status': p.get('status', ''),
            'start_date': p.get('start_date', ''),
            'end_date': p.get('end_date', ''),
            'due_date': p.get('due_date', ''),
            'ar': p.get('ar', ''),
            'subscription_type': p.get('subscription_type', '')
        })

    status_counts = Counter(p.get('status', 'Unknown') for p in payment_data)
    return jsonify({'rows': rows, 'total': len(rows), 'by_status': dict(status_counts)})


@app.route('/api/pipeline')
def get_pipeline():
    data = fetch_pipeline_data()
    return jsonify({'stages': data})


@app.route('/api/leads-monthly')
def get_leads_monthly():
    start_local, end_local = get_date_range_local()
    contacts = fetch_all_contacts()
    filtered = [c for c in contacts if in_range(parse_date(c.get('dateAdded')), start_local, end_local)]

    monthly = {}
    for c in filtered:
        dt = parse_date(c.get('dateAdded'))
        key = to_local_month(dt)
        if key:
            monthly[key] = monthly.get(key, 0) + 1

    sorted_months = sorted(monthly.items())
    return jsonify({'monthly': [{'month': m, 'count': v} for m, v in sorted_months]})


@app.route('/api/monthly-summary')
def get_monthly_summary():
    contacts = fetch_all_contacts()
    sales_data = fetch_sales_data()
    churn_data = fetch_churn_data()
    webhook_calls = load_calls()
    payment_data = fetch_payment_data()
    pipeline_data = fetch_pipeline_data()

    use_webhook = bool(webhook_calls)
    if not use_webhook:
        convos = fetch_conversations()

    months_data = {}

    for c in contacts:
        dt = parse_date(c.get('dateAdded'))
        key = to_local_month(dt)
        if key:
            if key not in months_data:
                months_data[key] = {'leads': 0, 'calls': 0, 'opps': 0, 'contracts': 0, 'revenue': 0, 'churn': 0}
            months_data[key]['leads'] += 1

    if use_webhook:
        for c in webhook_calls:
            dt = parse_date(c.get('timestamp'))
            key = to_local_month(dt)
            if key:
                if key not in months_data:
                    months_data[key] = {'leads': 0, 'calls': 0, 'opps': 0, 'contracts': 0, 'revenue': 0, 'churn': 0}
                months_data[key]['calls'] += 1
    else:
        for c in convos:
            dt = parse_date(c.get('dateAdded'))
            key = to_local_month(dt)
            if key:
                if key not in months_data:
                    months_data[key] = {'leads': 0, 'calls': 0, 'opps': 0, 'contracts': 0, 'revenue': 0, 'churn': 0}
                months_data[key]['calls'] += 1

    for s in sales_data:
        key = date_to_month(s.get('first_payment'))
        if key:
            if key not in months_data:
                months_data[key] = {'leads': 0, 'calls': 0, 'opps': 0, 'contracts': 0, 'revenue': 0, 'churn': 0}
            months_data[key]['contracts'] += 1
            months_data[key]['revenue'] += s.get('fee', 0)

    for c in churn_data:
        key = date_to_month(c.get('end_date'))
        if key:
            if key not in months_data:
                months_data[key] = {'leads': 0, 'calls': 0, 'opps': 0, 'contracts': 0, 'revenue': 0, 'churn': 0}
            months_data[key]['churn'] += 1

    try:
        opps_open = fetch_opportunities('open')
        opps_won = fetch_opportunities('won')
        all_opps = opps_open + opps_won
        for opp in all_opps:
            dt = parse_date(opp.get('createdAt'))
            key = to_local_month(dt)
            if key:
                if key not in months_data:
                    months_data[key] = {'leads': 0, 'calls': 0, 'opps': 0, 'contracts': 0, 'revenue': 0, 'churn': 0}
                months_data[key]['opps'] += 1
    except Exception:
        pass

    sorted_months = sorted((k, v) for k, v in months_data.items() if k >= '2025-06')

    active_clients = len([p for p in payment_data if p.get('status') == 'Active'])
    total_inactive = len(churn_data)
    total_contracts = sum(m['contracts'] for _, m in sorted_months)
    total_revenue = sum(m['revenue'] for _, m in sorted_months)
    total_leads = sum(m['leads'] for _, m in sorted_months)
    total_churn = sum(m['churn'] for _, m in sorted_months)

    avg_revenue = total_revenue / total_contracts if total_contracts > 0 else 0
    conversion = (total_contracts / total_leads * 100) if total_leads > 0 else 0
    net_growth = total_contracts - total_churn

    return jsonify({
        'monthly': [{
            'month': m,
            'leads': d['leads'],
            'calls': d['calls'],
            'opps': d['opps'],
            'contracts': d['contracts'],
            'revenue': round(d['revenue'], 2),
            'churn': d['churn']
        } for m, d in sorted_months],
        'kpis': {
            'avg_revenue_per_contract': round(avg_revenue, 2),
            'active_clients': active_clients,
            'total_inactive': total_inactive,
            'net_growth': net_growth,
            'total_contracts': total_contracts,
            'total_churn': total_churn,
            'lead_to_contract': round(conversion, 1),
            'total_revenue': round(total_revenue, 2)
        },
        'pipeline': pipeline_data,
        'calls_source': 'webhook' if use_webhook else 'conversations_api'
    })


@app.route('/monthly-summary')
def monthly_summary():
    return send_from_directory('static', 'monthly-summary.html')


@app.route('/sdr')
def sdr_page():
    return send_from_directory('static', 'sdr.html')


@app.route('/api/sdr')
def get_sdr_data():

    query = '''{ boards(ids: [1944309746]) {
        groups(ids: ["1733734937_book1_usmd_dec_new_Mjj2w4It", "new_group_mkkazbjx"]) {
        id title items_page(limit: 500) { items { id name
        column_values(ids: ["person", "status_Mjj2SLPt", "status", "status_1_Mjj1LR31",
        "date4", "date_mkm1w0x", "numbers_Mjivm65q", "text_MjivuCB8", "text_MjivTjDy",
        "text_MjivvUIB", "color_mm1q89vw", "color_mm1gq51r"]) { id text } } } } } }'''
    resp = monday_query(query)

    leads_group = []
    closed_group = []
    for group in resp['data']['boards'][0]['groups']:
        for item in group['items_page']['items']:
            rec = {'name': item['name'], 'group': group['title']}
            for cv in item['column_values']:
                rec[cv['id']] = cv.get('text', '') or ''
            if group['id'] == '1733734937_book1_usmd_dec_new_Mjj2w4It':
                leads_group.append(rec)
            else:
                closed_group.append(rec)

    all_deals = leads_group + closed_group

    rep_stats = {}
    for deal in all_deals:
        owner = deal.get('person', '') or 'Unassigned'
        if owner not in rep_stats:
            rep_stats[owner] = {'total_deals': 0, 'signed': 0, 'sent': 0, 'pending_payment': 0,
                                'paid': 0, 'revenue': 0, 'sources': Counter()}
        rep_stats[owner]['total_deals'] += 1
        contract = deal.get('status_1_Mjj1LR31', '')
        if contract == 'Signed':
            rep_stats[owner]['signed'] += 1
        elif contract == 'Sent':
            rep_stats[owner]['sent'] += 1
        payment = deal.get('status', '')
        if payment == 'PAID':
            rep_stats[owner]['paid'] += 1
        elif payment == 'Pending':
            rep_stats[owner]['pending_payment'] += 1
        fee = 0
        try:
            fee = float(deal.get('numbers_Mjivm65q', '') or 0)
        except ValueError:
            pass
        if payment == 'PAID':
            rep_stats[owner]['revenue'] += fee
        source = deal.get('status_Mjj2SLPt', '') or 'Unknown'
        rep_stats[owner]['sources'][source] += 1

    reps = []
    for owner, stats in sorted(rep_stats.items(), key=lambda x: x[1]['total_deals'], reverse=True):
        reps.append({
            'name': owner,
            'total_deals': stats['total_deals'],
            'signed': stats['signed'],
            'sent': stats['sent'],
            'paid': stats['paid'],
            'pending_payment': stats['pending_payment'],
            'revenue': round(stats['revenue'], 2),
            'conversion': round(stats['signed'] / stats['total_deals'] * 100, 1) if stats['total_deals'] > 0 else 0,
            'sources': dict(stats['sources'].most_common(10))
        })

    monthly = {}
    for deal in all_deals:
        date_sent = deal.get('date_mkm1w0x', '')
        key = date_to_month(date_sent)
        if key and key >= '2025-06':
            if key not in monthly:
                monthly[key] = {'contracts_sent': 0, 'signed': 0, 'paid': 0, 'revenue': 0}
            monthly[key]['contracts_sent'] += 1
            if deal.get('status_1_Mjj1LR31', '') == 'Signed':
                monthly[key]['signed'] += 1
            if deal.get('status', '') == 'PAID':
                monthly[key]['paid'] += 1
                try:
                    monthly[key]['revenue'] += float(deal.get('numbers_Mjivm65q', '') or 0)
                except ValueError:
                    pass

    sorted_monthly = sorted(monthly.items())

    source_breakdown = Counter()
    for deal in all_deals:
        source = deal.get('status_Mjj2SLPt', '') or 'Unknown'
        source_breakdown[source] += 1

    tier_breakdown = Counter()
    for deal in all_deals:
        tier = deal.get('color_mm1q89vw', '') or 'Not Set'
        tier_breakdown[tier] += 1

    sub_type_breakdown = Counter()
    for deal in all_deals:
        sub_type = deal.get('color_mm1gq51r', '') or 'Not Set'
        sub_type_breakdown[sub_type] += 1

    recent_deals = sorted(all_deals, key=lambda d: d.get('date_mkm1w0x', '') or '', reverse=True)[:20]
    recent_list = [{
        'business': d['name'],
        'contact': d.get('text_MjivuCB8', ''),
        'email': d.get('text_MjivTjDy', ''),
        'phone': d.get('text_MjivvUIB', ''),
        'contract_status': d.get('status_1_Mjj1LR31', ''),
        'payment_status': d.get('status', ''),
        'date_sent': d.get('date_mkm1w0x', ''),
        'source': d.get('status_Mjj2SLPt', ''),
        'owner': d.get('person', '')
    } for d in recent_deals]

    return jsonify({
        'reps': reps,
        'monthly': [{'month': m, **d} for m, d in sorted_monthly],
        'source_breakdown': dict(source_breakdown.most_common(10)),
        'tier_breakdown': dict(tier_breakdown.most_common(10)),
        'sub_type_breakdown': dict(sub_type_breakdown.most_common(10)),
        'recent_deals': recent_list
    })


@app.route('/api/refresh', methods=['POST'])
def refresh_cache():
    cache.clear()
    return jsonify({'status': 'ok', 'message': 'Cache cleared'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
