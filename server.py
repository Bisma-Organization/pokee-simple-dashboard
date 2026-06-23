from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import os
import json
import requests
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from collections import Counter

app = Flask(__name__, static_folder='static')
CORS(app)

GHL_TOKEN = os.environ.get('GHL_API_TOKEN', '')
GHL_LOCATION = os.environ.get('GHL_LOCATION_ID', '')
MONDAY_TOKEN = os.environ.get('MONDAY_API_TOKEN', '')
STRIPE_KEY = os.environ.get('STRIPE_API_KEY', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'usmd-calls-2024')
LOCAL_TZ = ZoneInfo(os.environ.get('DASHBOARD_TIMEZONE', 'America/Los_Angeles'))

GHL_BASE = 'https://services.leadconnectorhq.com'
MONDAY_BASE = 'https://api.monday.com/v2'
STRIPE_BASE = 'https://api.stripe.com/v1'

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
        import calendar
        query = '''{ boards(ids: [1944525313]) {
            groups(ids: ["1733660267_book1_usmd_dec_new_Mjj1XfHC"]) {
            items_page(limit: 500) { items { id name
            column_values(ids: ["mirror_Mjj1ZRum", "numeric_mkty8mvs", "lookup_mkq2tmcq",
            "lookup_mm2cybv2", "status_Mjj1A9wh"]) { id text
            ... on MirrorValue { display_value } } } } } } }'''
        resp = monday_query(query)
        items = resp['data']['boards'][0]['groups'][0]['items_page']['items']

        today = datetime.now(LOCAL_TZ).date()
        records = []
        for item in items:
            last_payment = ''
            due_date = ''
            fee = ''
            sub_type = ''
            status = ''
            for cv in item['column_values']:
                cid = cv['id']
                val = cv.get('display_value') or cv.get('text') or ''
                if cid == 'mirror_Mjj1ZRum':
                    last_payment = val[:10] if val else ''
                elif cid == 'numeric_mkty8mvs':
                    due_date = val
                elif cid == 'lookup_mkq2tmcq':
                    fee = val
                elif cid == 'lookup_mm2cybv2':
                    sub_type = val
                elif cid == 'status_Mjj1A9wh':
                    status = val

            late_days = None
            if last_payment and sub_type:
                try:
                    last_pay_date = datetime.strptime(last_payment, '%Y-%m-%d').date()
                    if sub_type == '28d':
                        next_due = last_pay_date + timedelta(days=28)
                    else:
                        next_month = last_pay_date.month + 1
                        next_year = last_pay_date.year
                        if next_month > 12:
                            next_month = 1
                            next_year += 1
                        max_day = calendar.monthrange(next_year, next_month)[1]
                        next_due = last_pay_date.replace(year=next_year, month=next_month, day=min(last_pay_date.day, max_day))
                    if today > next_due:
                        late_days = (today - next_due).days
                except (ValueError, TypeError):
                    pass

            records.append({
                'name': item['name'],
                'last_payment': last_payment,
                'due_date': due_date,
                'subscription_fee': fee,
                'subscription_type': sub_type,
                'status': status,
                'late_days': late_days
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

    rows = []
    for p in payment_data:
        rows.append({
            'name': p.get('name', ''),
            'subscription_type': p.get('subscription_type', ''),
            'last_payment': p.get('last_payment', ''),
            'due_date': p.get('due_date', ''),
            'subscription_fee': p.get('subscription_fee', ''),
            'status': p.get('status', ''),
            'late_days': p.get('late_days')
        })

    return jsonify({'rows': rows, 'total': len(rows)})


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


STRIPE_ANALYTICS_URL = 'https://api.stripe.com/v2/data/analytics/metric_query'
STRIPE_API_VERSION = '2026-04-22.preview'


def stripe_get(endpoint, params=None):
    if not STRIPE_KEY:
        raise ValueError('STRIPE_API_KEY environment variable is not set')
    resp = requests.get(
        f'{STRIPE_BASE}{endpoint}',
        auth=(STRIPE_KEY, ''),
        params=params
    )
    resp.raise_for_status()
    return resp.json()


def stripe_analytics_query(metrics, starts_at, ends_at, granularity='month'):
    if not STRIPE_KEY:
        raise ValueError('STRIPE_API_KEY environment variable is not set')
    headers = {
        'Authorization': f'Bearer {STRIPE_KEY}',
        'Stripe-Version': STRIPE_API_VERSION,
        'Content-Type': 'application/json'
    }
    payload = {
        'metrics': [{'name': m} for m in metrics],
        'starts_at': starts_at,
        'ends_at': ends_at,
        'granularity': granularity,
        'currency': 'usd',
        'timezone': 'America/Los_Angeles'
    }
    resp = requests.post(STRIPE_ANALYTICS_URL, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()


def fetch_stripe_mrr_arr(starts_at, ends_at, granularity='month'):
    cache_key = f'stripe_analytics_{starts_at}_{ends_at}_{granularity}'

    def _fetch():
        data = stripe_analytics_query(
            ['revenue.mrr', 'revenue.arr'],
            starts_at, ends_at, granularity
        )
        return data
    return cached(cache_key, _fetch)


def fetch_stripe_subscriptions():
    def _fetch():
        all_subs = []
        for status in ['active', 'past_due']:
            params = {'limit': 100, 'status': status}
            while True:
                data = stripe_get('/subscriptions', params)
                subs = data.get('data', [])
                all_subs.extend(subs)
                if not data.get('has_more'):
                    break
                params['starting_after'] = subs[-1]['id']
        return all_subs
    return cached('stripe_subs', _fetch)


def fetch_stripe_charges(start_ts, end_ts):
    cache_key = f'stripe_charges_{start_ts}_{end_ts}'

    def _fetch():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from calendar import monthrange

        if not start_ts or not end_ts:
            return _fetch_charges_range(start_ts, end_ts)

        chunks = []
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        end_dt_utc = datetime.fromtimestamp(end_ts, tz=timezone.utc)
        while dt <= end_dt_utc:
            chunk_start = int(dt.timestamp())
            _, days = monthrange(dt.year, dt.month)
            next_month = dt.replace(day=days, hour=23, minute=59, second=59)
            chunk_end = min(int(next_month.timestamp()), end_ts)
            chunks.append((chunk_start, chunk_end))
            if dt.month == 12:
                dt = dt.replace(year=dt.year + 1, month=1, day=1)
            else:
                dt = dt.replace(month=dt.month + 1, day=1)

        all_charges = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_fetch_charges_range, s, e): (s, e) for s, e in chunks}
            for future in as_completed(futures):
                all_charges.extend(future.result())
        return all_charges
    return cached(cache_key, _fetch)


def _fetch_charges_range(start_ts, end_ts):
    all_charges = []
    params = {'limit': 100}
    if start_ts:
        params['created[gte]'] = start_ts
    if end_ts:
        params['created[lte]'] = end_ts
    while True:
        data = stripe_get('/charges', params)
        charges = data.get('data', [])
        all_charges.extend(charges)
        if not data.get('has_more'):
            break
        params['starting_after'] = charges[-1]['id']
    return all_charges


def calc_revenue(charges):
    total = 0
    for ch in charges:
        if ch.get('paid') and ch.get('status') == 'succeeded':
            total += ch.get('amount', 0) - ch.get('amount_refunded', 0)
    return round(total / 100, 2)


def fetch_stripe_balance_txns(start_ts, end_ts):
    cache_key = f'stripe_bal_txns_{start_ts}_{end_ts}'

    def _fetch():
        all_txns = []
        params = {'limit': 100}
        if start_ts:
            params['created[gte]'] = start_ts
        if end_ts:
            params['created[lte]'] = end_ts
        while True:
            data = stripe_get('/balance_transactions', params)
            txns = data.get('data', [])
            all_txns.extend(txns)
            if not data.get('has_more'):
                break
            params['starting_after'] = txns[-1]['id']
        return all_txns
    return cached(cache_key, _fetch)


def fetch_stripe_invoices(start_ts, end_ts):
    """Fetch all paid invoices in date range using invoice created date (matches Billing overview)."""
    cache_key = f'stripe_invoices_{start_ts}_{end_ts}'

    def _fetch():
        all_invoices = []
        params = {
            'limit': 100,
            'status': 'paid',
            'created[gte]': start_ts,
            'created[lte]': end_ts
        }
        while True:
            data = stripe_get('/invoices', params)
            invoices = data.get('data', [])
            all_invoices.extend(invoices)
            if not data.get('has_more'):
                break
            params['starting_after'] = invoices[-1]['id']
        return all_invoices
    return cached(cache_key, _fetch)


def fetch_stripe_credit_notes(start_ts, end_ts):
    """Fetch credit notes in date range."""
    cache_key = f'stripe_credit_notes_{start_ts}_{end_ts}'

    def _fetch():
        all_notes = []
        params = {
            'limit': 100,
            'created[gte]': start_ts,
            'created[lte]': end_ts
        }
        while True:
            data = stripe_get('/credit_notes', params)
            notes = data.get('data', [])
            all_notes.extend(notes)
            if not data.get('has_more'):
                break
            params['starting_after'] = notes[-1]['id']
        return all_notes
    return cached(cache_key, _fetch)


def calc_net_volume_v2(start_ts, end_ts):
    """Net volume from invoices (matches Stripe Billing overview)."""
    invoices = fetch_stripe_invoices(start_ts, end_ts)
    credit_notes = fetch_stripe_credit_notes(start_ts, end_ts)

    total_net = 0
    daily_net = {}

    for inv in invoices:
        amount = inv.get('amount_paid', 0)
        created = inv.get('created', 0)
        dt = datetime.fromtimestamp(created, tz=LOCAL_TZ)
        key = dt.strftime('%Y-%m-%d')
        total_net += amount
        daily_net[key] = daily_net.get(key, 0) + amount

    for cn in credit_notes:
        amount = cn.get('amount', 0)
        created = cn.get('created', 0)
        dt = datetime.fromtimestamp(created, tz=LOCAL_TZ)
        key = dt.strftime('%Y-%m-%d')
        total_net -= amount
        daily_net[key] = daily_net.get(key, 0) - amount

    return {
        'net': round(total_net / 100, 2),
        'daily_net': {k: round(v / 100, 2) for k, v in daily_net.items()}
    }


def fetch_all_canceled_subs():
    """Fetch all canceled subscriptions (cached). Filter by canceled_at separately."""
    def _fetch():
        all_subs = []
        params = {'limit': 100, 'status': 'canceled'}
        while True:
            data = stripe_get('/subscriptions', params)
            subs = data.get('data', [])
            all_subs.extend(subs)
            if not data.get('has_more'):
                break
            params['starting_after'] = subs[-1]['id']
        return all_subs
    return cached('stripe_all_canceled', _fetch)


def fetch_stripe_canceled_via_events(start_ts, end_ts):
    """Use Events API for recent cancellations (< 30 days old)."""
    cache_key = f'stripe_canceled_events_{start_ts}_{end_ts}'

    def _fetch():
        all_subs = []
        params = {'limit': 100, 'type': 'customer.subscription.deleted'}
        if start_ts:
            params['created[gte]'] = start_ts
        if end_ts:
            params['created[lte]'] = end_ts
        while True:
            data = stripe_get('/events', params)
            events = data.get('data', [])
            for event in events:
                sub = event.get('data', {}).get('object', {})
                if sub:
                    all_subs.append(sub)
            if not data.get('has_more'):
                break
            params['starting_after'] = events[-1]['id']
        return all_subs
    return cached(cache_key, _fetch)


def fetch_stripe_paused_subs():
    """Fetch all currently paused subscriptions."""
    def _fetch():
        all_subs = []
        params = {'limit': 100, 'status': 'active'}
        while True:
            data = stripe_get('/subscriptions', params)
            subs = data.get('data', [])
            for sub in subs:
                if sub.get('pause_collection'):
                    all_subs.append(sub)
            if not data.get('has_more'):
                break
            params['starting_after'] = subs[-1]['id']
        return all_subs
    return cached('stripe_paused_subs', _fetch)


def fetch_invoices_for_period(start_ts, end_ts):
    """Fetch all paid invoices in a period."""
    cache_key = f'stripe_invoices_{start_ts}_{end_ts}'
    def _fetch():
        all_invoices = []
        params = {'limit': 100, 'status': 'paid'}
        if start_ts:
            params['created[gte]'] = start_ts
        if end_ts:
            params['created[lte]'] = end_ts
        while True:
            data = stripe_get('/invoices', params)
            invoices = data.get('data', [])
            all_invoices.extend(invoices)
            if not data.get('has_more'):
                break
            params['starting_after'] = invoices[-1]['id']
        return all_invoices
    return cached(cache_key, _fetch)


def sub_mrr_from_items(sub):
    """Calculate normalized monthly MRR from subscription items."""
    mrr = 0
    for item in sub.get('items', {}).get('data', []):
        price = item.get('price', {})
        amount = price.get('unit_amount', 0) * item.get('quantity', 1)
        interval = price.get('recurring', {}).get('interval', 'month')
        interval_count = price.get('recurring', {}).get('interval_count', 1)
        if interval == 'month':
            mrr += amount / interval_count
        elif interval == 'year':
            mrr += amount / (12 * interval_count)
        elif interval == 'day':
            mrr += amount * 30.4375 / interval_count
    return mrr


def calc_full_churn(start_ts, end_ts):
    """Calculate churn revenue using Stripe v2 Analytics API.

    Uses daily granularity for ranges <= 93 days, monthly for longer ranges.
    """
    start_dt = datetime.fromtimestamp(start_ts, tz=LOCAL_TZ)
    end_dt = datetime.fromtimestamp(end_ts, tz=LOCAL_TZ)

    day_count = (end_dt - start_dt).days + 1
    granularity = 'day' if day_count <= 93 else 'month'

    starts_at = start_dt.strftime('%Y-%m-%dT00:00:00Z')
    ends_at = (end_dt + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')
    utc_now = datetime.now(timezone.utc)
    if datetime.fromisoformat(ends_at.replace('Z', '+00:00')) > utc_now:
        ends_at = utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')

    cache_key = f'stripe_churn_v2_{starts_at}_{ends_at}_{granularity}'

    def _fetch():
        headers = {
            'Authorization': f'Bearer {STRIPE_KEY}',
            'Stripe-Version': STRIPE_API_VERSION,
            'Content-Type': 'application/json'
        }
        payload = {
            'metrics': [{'name': 'revenue_growth.mrr'}],
            'starts_at': starts_at,
            'ends_at': ends_at,
            'granularity': granularity,
            'currency': 'usd',
            'filters': {'change_type': ['MRR_CHURN', 'MRR_CONTRACTION']},
            'group_by': ['change_type']
        }
        resp = requests.post(STRIPE_ANALYTICS_URL, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()

    data = cached(cache_key, _fetch)

    start_date = start_dt.strftime('%Y-%m-%d')
    end_date = end_dt.strftime('%Y-%m-%d')

    total_churn = 0
    total_contraction = 0
    monthly_churn = {}
    daily_churn = {}

    for item in data.get('data', []):
        ts = item.get('timestamp', '')[:10]
        if ts < start_date or ts > end_date:
            continue
        month = ts[:7]
        change_type = item.get('dimensions', {}).get('change_type', '')
        for r in item.get('results', []):
            val = abs(int(r.get('value') or 0))
            if change_type == 'MRR_CHURN':
                total_churn += val
            elif change_type == 'MRR_CONTRACTION':
                total_contraction += val
            monthly_churn[month] = monthly_churn.get(month, 0) + val
            daily_churn[ts] = daily_churn.get(ts, 0) + val

    total_lost = total_churn + total_contraction

    canceled = fetch_stripe_canceled(start_ts, end_ts)
    churn_count = len(canceled)

    return {
        'lost_mrr': round(total_lost / 100, 2),
        'cancellation_mrr': round(total_churn / 100, 2),
        'contraction_mrr': round(total_contraction / 100, 2),
        'pause_mrr': 0,
        'canceled_count': churn_count,
        'monthly_churn_mrr': {k: round(v / 100, 2) for k, v in monthly_churn.items()},
        'daily_churn_mrr': {k: round(v / 100, 2) for k, v in daily_churn.items()},
        'source': 'v2_analytics'
    }


def fetch_stripe_canceled(start_ts, end_ts):
    """Get subscriptions canceled within the given time range.
    Uses Events API for recent data (accurate, includes context),
    falls back to subscriptions list for older periods."""
    now_ts = int(time.time())
    events_cutoff = now_ts - 25 * 86400

    if start_ts and start_ts >= events_cutoff:
        subs = fetch_stripe_canceled_via_events(start_ts, end_ts)
        active_subs = fetch_stripe_subscriptions()
        active_customers = {s.get('customer') for s in active_subs if s.get('status') in ('active', 'past_due')}
        return [sub for sub in subs if sub.get('customer') not in active_customers]

    all_canceled = fetch_all_canceled_subs()
    filtered = []
    for sub in all_canceled:
        canceled_at = sub.get('canceled_at') or sub.get('ended_at')
        if not canceled_at:
            continue
        if start_ts and canceled_at < start_ts:
            continue
        if end_ts and canceled_at > end_ts:
            continue
        filtered.append(sub)
    return filtered


@app.route('/api/stripe/mrr')
def get_stripe_mrr():
    try:
        now = datetime.now(LOCAL_TZ)
        utc_now = datetime.now(timezone.utc)
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        starts_at = start_of_month.strftime('%Y-%m-%dT%H:%M:%SZ')
        ends_at = utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')

        analytics = fetch_stripe_mrr_arr(starts_at, ends_at, 'month')
        mrr = 0
        arr = 0
        for item in analytics.get('data', []):
            for r in item.get('results', []):
                val = int(r.get('value', 0)) / 100
                if r.get('name') == 'revenue.mrr':
                    mrr = val
                elif r.get('name') == 'revenue.arr':
                    arr = val

        subs = fetch_stripe_subscriptions()
        active_count = len([s for s in subs if s.get('status') == 'active' and not s.get('pause_collection')])
        past_due_count = len([s for s in subs if s.get('status') == 'past_due'])
        paused_count = len([s for s in subs if s.get('pause_collection')])
        return jsonify({
            'mrr': mrr,
            'arr': arr,
            'active': active_count,
            'past_due': past_due_count,
            'paused': paused_count
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stripe/revenue')
def get_stripe_revenue():
    try:
        start_str = request.args.get('start')
        end_str = request.args.get('end')

        start_ts = None
        end_ts = None
        if start_str:
            start_dt = datetime.fromisoformat(start_str).replace(tzinfo=LOCAL_TZ)
            start_ts = int(start_dt.timestamp())
        if end_str:
            end_dt = datetime.fromisoformat(end_str).replace(hour=23, minute=59, second=59, tzinfo=LOCAL_TZ)
            end_ts = int(end_dt.timestamp())

        charges = fetch_stripe_charges(start_ts, end_ts)
        revenue = calc_revenue(charges)

        monthly = {}
        for ch in charges:
            if ch.get('paid') and ch.get('status') == 'succeeded':
                dt = datetime.fromtimestamp(ch['created'], tz=LOCAL_TZ)
                key = dt.strftime('%Y-%m')
                net = (ch.get('amount', 0) - ch.get('amount_refunded', 0)) / 100
                monthly[key] = monthly.get(key, 0) + net

        sorted_months = sorted(monthly.items())
        return jsonify({
            'revenue': revenue,
            'monthly': [{'month': m, 'revenue': round(v, 2)} for m, v in sorted_months],
            'charge_count': len([c for c in charges if c.get('paid') and c.get('status') == 'succeeded'])
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stripe/churn')
def get_stripe_churn():
    try:
        start_str = request.args.get('start')
        end_str = request.args.get('end')

        start_ts = None
        end_ts = None
        if start_str:
            start_dt = datetime.fromisoformat(start_str).replace(tzinfo=LOCAL_TZ)
            start_ts = int(start_dt.timestamp())
        if end_str:
            end_dt = datetime.fromisoformat(end_str).replace(hour=23, minute=59, second=59, tzinfo=LOCAL_TZ)
            end_ts = int(end_dt.timestamp())

        canceled = fetch_stripe_canceled(start_ts, end_ts)

        monthly = {}
        for sub in canceled:
            canceled_at = sub.get('canceled_at') or sub.get('ended_at')
            if canceled_at:
                dt = datetime.fromtimestamp(canceled_at, tz=LOCAL_TZ)
                if start_ts and canceled_at < start_ts:
                    continue
                if end_ts and canceled_at > end_ts:
                    continue
                key = dt.strftime('%Y-%m')
                monthly[key] = monthly.get(key, 0) + 1

        lost_mrr = 0
        for sub in canceled:
            canceled_at = sub.get('canceled_at') or sub.get('ended_at')
            if canceled_at:
                if start_ts and canceled_at < start_ts:
                    continue
                if end_ts and canceled_at > end_ts:
                    continue
                for item in sub.get('items', {}).get('data', []):
                    price = item.get('price', {})
                    amount = price.get('unit_amount', 0) * item.get('quantity', 1)
                    interval = price.get('recurring', {}).get('interval', 'month')
                    interval_count = price.get('recurring', {}).get('interval_count', 1)
                    if interval == 'month':
                        lost_mrr += amount / interval_count
                    elif interval == 'year':
                        lost_mrr += amount / (12 * interval_count)
                    elif interval == 'day':
                        lost_mrr += amount * 30.4375 / interval_count

        sorted_months = sorted(monthly.items())
        return jsonify({
            'total_canceled': sum(monthly.values()),
            'lost_mrr': round(lost_mrr / 100, 2),
            'monthly': [{'month': m, 'count': v} for m, v in sorted_months]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stripe/summary')
def get_stripe_summary():
    try:
        resp = _stripe_summary_impl()
        resp.headers['Cache-Control'] = 'no-cache, no-store'
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _stripe_summary_impl():
    start_str = request.args.get('start')
    end_str = request.args.get('end')

    now = datetime.now(LOCAL_TZ)
    if start_str:
        start_dt = datetime.fromisoformat(start_str).replace(tzinfo=LOCAL_TZ)
    else:
        start_dt = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if end_str:
        end_dt = datetime.fromisoformat(end_str).replace(hour=23, minute=59, second=59, tzinfo=LOCAL_TZ)
    else:
        end_dt = now

    starts_at_iso = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    utc_now = datetime.now(timezone.utc)
    ends_at_candidate = (end_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, tzinfo=timezone.utc)
    if ends_at_candidate > utc_now:
        ends_at_candidate = utc_now
    ends_at_iso = ends_at_candidate.strftime('%Y-%m-%dT%H:%M:%SZ')

    analytics_data = fetch_stripe_mrr_arr(starts_at_iso, ends_at_iso, 'month')
    mrr_monthly = {}
    arr_monthly = {}
    latest_mrr = 0
    latest_arr = 0
    for item in analytics_data.get('data', []):
        ts = item.get('timestamp', '')[:7]
        for r in item.get('results', []):
            val = int(r.get('value') or 0) / 100
            if r.get('name') == 'revenue.mrr':
                mrr_monthly[ts] = val
                latest_mrr = val
            elif r.get('name') == 'revenue.arr':
                arr_monthly[ts] = val
                latest_arr = val

    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    rev_data = calc_net_volume_v2(start_ts, end_ts)

    churn_data = calc_full_churn(start_ts, end_ts)
    churn_count = churn_data['canceled_count']
    lost_mrr_dollars = churn_data['lost_mrr']
    churn_source = churn_data['source']

    daily_net_v2 = rev_data.get('daily_net', {})
    daily_churn_mrr = churn_data.get('daily_churn_mrr', {})

    # Build daily chart data
    all_dates = sorted(set(list(daily_net_v2.keys()) + list(daily_churn_mrr.keys())))
    monthly_data = []
    for d in all_dates:
        monthly_data.append({
            'date': d,
            'month': d[:7],
            'net_volume': daily_net_v2.get(d, 0),
            'churn_revenue': daily_churn_mrr.get(d, 0)
        })

    subs = fetch_stripe_subscriptions()
    active_count = len([s for s in subs if s.get('status') == 'active' and not s.get('pause_collection')])
    past_due_count = len([s for s in subs if s.get('status') == 'past_due'])
    paused_count = len([s for s in subs if s.get('pause_collection')])

    churn_rate = round((churn_count / (active_count + churn_count) * 100), 1) if (active_count + churn_count) > 0 else 0

    return jsonify({
        'mrr': latest_mrr,
        'arr': latest_arr,
        'net_volume': rev_data['net'],
        'churn_count': churn_count,
        'lost_mrr': lost_mrr_dollars,
        'churn_rate': churn_rate,
        'churn_source': churn_source,
        'contraction_mrr': churn_data.get('contraction_mrr', 0),
        'active': active_count,
        'past_due': past_due_count,
        'paused': paused_count,
        'monthly': monthly_data,
        'mrr_monthly': mrr_monthly,
        'arr_monthly': arr_monthly
    })


@app.route('/api/stripe/test-net-metric')
def test_net_metric():
    """Debug: check balance_txns count, timestamps, and amounts for April."""
    april_start = int(datetime(2025, 4, 1, tzinfo=LOCAL_TZ).timestamp())
    april_end = int(datetime(2025, 4, 30, 23, 59, 59, tzinfo=LOCAL_TZ).timestamp())
    may_start = int(datetime(2025, 5, 1, tzinfo=LOCAL_TZ).timestamp())
    may_end = int(datetime(2025, 5, 31, 23, 59, 59, tzinfo=LOCAL_TZ).timestamp())

    VOLUME_TYPES = ('charge', 'payment', 'refund', 'payment_refund', 'adjustment')

    april_bal = fetch_stripe_balance_txns(april_start, april_end)
    april_types = {}
    for t in april_bal:
        tp = t['type']
        april_types[tp] = april_types.get(tp, 0) + t['net']
    april_filtered = sum(v for k, v in april_types.items() if k in VOLUME_TYPES)

    may_bal = fetch_stripe_balance_txns(may_start, may_end)
    may_types = {}
    for t in may_bal:
        tp = t['type']
        may_types[tp] = may_types.get(tp, 0) + t['net']
    may_filtered = sum(v for k, v in may_types.items() if k in VOLUME_TYPES)

    return jsonify({
        'april': {
            'start_ts': april_start,
            'end_ts': april_end,
            'total_txns': len(april_bal),
            'net_filtered': round(april_filtered / 100, 2),
            'expected': 108338.51,
            'types': {k: round(v / 100, 2) for k, v in sorted(april_types.items(), key=lambda x: -abs(x[1]))}
        },
        'may': {
            'start_ts': may_start,
            'end_ts': may_end,
            'total_txns': len(may_bal),
            'net_filtered': round(may_filtered / 100, 2),
            'expected': 118357.51,
            'types': {k: round(v / 100, 2) for k, v in sorted(may_types.items(), key=lambda x: -abs(x[1]))}
        }
    })


@app.route('/stripe-dashboard')
def stripe_dashboard():
    resp = send_from_directory('static', 'stripe-dashboard.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.headers['ETag'] = 'v7-net-volume-v2'
    return resp


@app.route('/api/refresh', methods=['POST'])
def refresh_cache():
    cache.clear()
    return jsonify({'status': 'ok', 'message': 'Cache cleared'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
