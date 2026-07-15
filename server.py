from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import os
import json
import requests
import time
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from collections import Counter
from pymongo import MongoClient

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
            try:
                resp = ghl_get('/contacts/', params)
            except Exception:
                break
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
            column_values(ids: ["color_mm31q77v", "numbers_Mjivm65q", "date4", "date_Mjiv0T8Z", "status",
            "text_MjivuCB8", "text_MjivTjDy"]) { id text column { title } } } } } } }'''
        resp = monday_query(query)
        records = []
        for group in resp['data']['boards'][0]['groups']:
            for item in group['items_page']['items']:
                new_fee_text = ''
                old_fee_text = ''
                first_payment = ''
                last_payment = ''
                status = ''
                contact = ''
                email = ''
                for cv in item['column_values']:
                    cid = cv['id']
                    text = cv.get('text', '') or ''
                    if cid == 'color_mm31q77v':
                        new_fee_text = text
                    elif cid == 'numbers_Mjivm65q':
                        old_fee_text = text
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
                fee = 0
                if new_fee_text:
                    try:
                        fee = float(new_fee_text.replace('$', '').replace(',', ''))
                    except ValueError:
                        pass
                if fee == 0 and old_fee_text:
                    try:
                        fee = float(old_fee_text)
                    except ValueError:
                        pass
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


def calc_net_volume(balance_txns):
    """Net volume = sum of net for charge/payment/refund/adjustment types (matches Stripe dashboard)."""
    VOLUME_TYPES = ('charge', 'payment', 'refund', 'payment_refund', 'adjustment')
    net_volume = 0
    gross = 0
    fees = 0
    refunds = 0
    for t in balance_txns:
        tp = t['type']
        if tp in VOLUME_TYPES:
            net_volume += t['net']
        if tp in ('charge', 'payment'):
            gross += t['amount']
            fees += t['fee']
        elif tp in ('refund', 'payment_refund'):
            refunds += abs(t['amount'])
    return {
        'gross': round(gross / 100, 2),
        'fees': round(fees / 100, 2),
        'refunds': round(refunds / 100, 2),
        'net': round(net_volume / 100, 2)
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

    bal_txns = fetch_stripe_balance_txns(start_ts, end_ts)
    rev_data = calc_net_volume(bal_txns)

    churn_data = calc_full_churn(start_ts, end_ts)
    churn_count = churn_data['canceled_count']
    lost_mrr_dollars = churn_data['lost_mrr']
    churn_source = churn_data['source']

    # Group net volume by date for chart
    VOLUME_TYPES = ('charge', 'payment', 'refund', 'payment_refund', 'adjustment')
    daily_net = {}
    for t in bal_txns:
        if t['type'] in VOLUME_TYPES:
            dt = datetime.fromtimestamp(t['created'], tz=LOCAL_TZ)
            key = dt.strftime('%Y-%m-%d')
            daily_net[key] = daily_net.get(key, 0) + t['net']

    daily_churn_mrr = churn_data.get('daily_churn_mrr', {})

    # Build daily chart data
    all_dates = sorted(set(list(daily_net.keys()) + list(daily_churn_mrr.keys())))
    monthly_data = []
    for d in all_dates:
        monthly_data.append({
            'date': d,
            'month': d[:7],
            'net_volume': round(daily_net.get(d, 0) / 100, 2),
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
        'gross_revenue': rev_data['gross'],
        'fees': rev_data['fees'],
        'refunds': rev_data['refunds'],
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


@app.route('/stripe-dashboard')
def stripe_dashboard():
    resp = send_from_directory('static', 'stripe-dashboard.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.headers['ETag'] = 'v6-net-volume-fix'
    return resp


@app.route('/api/refresh', methods=['POST'])
def refresh_cache():
    cache.clear()
    return jsonify({'status': 'ok', 'message': 'Cache cleared'})


# --- Aesthetic Record Reverse Proxy ---

AR_BASE = 'https://app.aestheticrecord.com'
AR_DOMAINS = ('app.aestheticrecord.com', 'api.aestheticrecord.com', 'aestheticrecord.com')

_ar_session = requests.Session()
_ar_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
})

@app.route('/ar')
def ar_page():
    return send_from_directory('static', 'ar.html')


@app.route('/proxy/ar/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/proxy/ar/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def ar_proxy(path):
    from urllib.parse import urlparse
    from flask import Response, make_response

    target_url = f'{AR_BASE}/{path}'
    if request.query_string:
        target_url += '?' + request.query_string.decode()

    skip_headers = ('host', 'connection', 'transfer-encoding', 'content-length',
                    'accept-encoding', 'origin', 'referer')
    headers = {}
    for key, val in request.headers:
        if key.lower() not in skip_headers:
            headers[key] = val
    headers['Host'] = 'app.aestheticrecord.com'
    headers['Origin'] = AR_BASE
    headers['Referer'] = AR_BASE + '/'
    headers['Accept-Encoding'] = 'identity'

    try:
        resp = _ar_session.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            allow_redirects=False,
            timeout=30,
            stream=False
        )
    except Exception as e:
        return f'<html><body><h2>Proxy Error</h2><p>{e}</p></body></html>', 502

    excluded_headers = ('transfer-encoding', 'content-encoding', 'content-length',
                        'connection', 'keep-alive', 'x-frame-options',
                        'content-security-policy', 'content-security-policy-report-only',
                        'strict-transport-security', 'x-content-type-options')
    response_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excluded_headers]

    content_type = resp.headers.get('Content-Type', '')

    def rewrite_urls(text):
        for domain in AR_DOMAINS:
            text = text.replace(f'https://{domain}', '/proxy/ar')
            text = text.replace(f'http://{domain}', '/proxy/ar')
            text = text.replace(f'//{domain}', '/proxy/ar')
            text = text.replace(f'"{domain}"', f'"{request.host}"')
            text = text.replace(f"'{domain}'", f"'{request.host}'")
        return text

    if any(ct in content_type for ct in ('text/html', 'javascript', 'application/json', 'text/css')):
        text = resp.text
        text = rewrite_urls(text)
        if 'text/html' in content_type and '<head' in text:
            base_tag = f'<base href="/proxy/ar/">'
            text = text.replace('<head>', '<head>' + base_tag, 1)
            text = text.replace('<HEAD>', '<HEAD>' + base_tag, 1)
        content = text.encode('utf-8')
    else:
        content = resp.content

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get('Location', '')
        if location:
            parsed = urlparse(location)
            if parsed.hostname and any(d in parsed.hostname for d in AR_DOMAINS):
                new_path = parsed.path or '/'
                if parsed.query:
                    new_path += '?' + parsed.query
                location = '/proxy/ar' + new_path
            elif location.startswith('/'):
                location = '/proxy/ar' + location
            response_headers = [(k, v) for k, v in response_headers if k.lower() != 'location']
            response_headers.append(('Location', location))

    proxy_resp = Response(content, status=resp.status_code, headers=response_headers)
    proxy_resp.headers['Content-Type'] = content_type
    for cookie_name, cookie_val in resp.cookies.items():
        proxy_resp.set_cookie(cookie_name, cookie_val, path='/proxy/ar/')

    return proxy_resp


# --- Email Report ---

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
REPORT_TO = 'anfobi@gmail.com'
REPORT_BCC = 'shoaibhasnat@systemheuristics.com,odeguzman@usmedicaldirectors.com'


def compute_kpis_for_range(start_dt, end_dt):
    # GHL timestamps are full datetimes — compare in Pacific
    start_local = start_dt.replace(tzinfo=LOCAL_TZ)
    end_local = end_dt.replace(hour=23, minute=59, second=59, tzinfo=LOCAL_TZ)

    # Monday.com fields are date-only strings parsed as midnight UTC — compare in UTC
    start_utc = start_dt.replace(tzinfo=timezone.utc)
    end_utc = end_dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

    leads_count = None
    sales_count = None
    total_revenue = None
    churn_count = None
    calls_count = None

    try:
        contacts = fetch_all_contacts()
        leads_count = len([c for c in contacts if in_range(parse_date(c.get('dateAdded')), start_local, end_local)])
    except Exception:
        pass

    try:
        sales_data = fetch_sales_data()
        sales = [s for s in sales_data if in_range(parse_date(s.get('first_payment')), start_utc, end_utc)]
        sales_count = len(sales)
        total_revenue = round(sum(s['fee'] for s in sales), 2)
    except Exception:
        pass

    try:
        churn_data_raw = fetch_churn_data()
        churn_count = len([c for c in churn_data_raw if in_range(parse_date(c.get('end_date')), start_utc, end_utc)])
    except Exception:
        pass

    try:
        webhook_calls = load_calls()
        if webhook_calls:
            calls_count = len([c for c in webhook_calls if in_range(parse_date(c.get('timestamp')), start_local, end_local)])
        else:
            convos = fetch_conversations()
            calls_count = len([c for c in convos if in_range(parse_date(c.get('dateAdded')), start_local, end_local)])
    except Exception:
        pass

    if sales_count is not None and total_revenue is not None and sales_count > 0:
        avg_per_sale = round(total_revenue / sales_count, 2)
    elif sales_count == 0:
        avg_per_sale = 0
    else:
        avg_per_sale = None

    return {
        'leads': leads_count,
        'sales': sales_count,
        'churn': churn_count,
        'calls': calls_count,
        'revenue': total_revenue,
        'avg_per_sale': avg_per_sale
    }


def generate_report_html():
    today = datetime.now(LOCAL_TZ).date()

    # Current 7 days
    cur7_start = today - timedelta(days=6)
    cur7_end = today

    # Previous 7 days
    prev7_start = today - timedelta(days=13)
    prev7_end = today - timedelta(days=7)

    # Rolling 30 days: current = last 30 days, previous = 30 days before that
    cur_month_start = today - timedelta(days=29)
    cur_month_end = today
    prev_month_start = today - timedelta(days=59)
    prev_month_end = today - timedelta(days=30)

    # Rolling 90 days: current = last 90 days, previous = 90 days before that
    cur_q_start = today - timedelta(days=89)
    cur_q_end = today
    prev_q_start = today - timedelta(days=179)
    prev_q_end = today - timedelta(days=90)

    periods = {
        'cur7': (cur7_start, cur7_end),
        'prev7': (prev7_start, prev7_end),
        'cur_month': (cur_month_start, cur_month_end),
        'prev_month': (prev_month_start, prev_month_end),
        'cur_quarter': (cur_q_start, cur_q_end),
        'prev_quarter': (prev_q_start, prev_q_end),
    }

    data = {}
    for key, (s, e) in periods.items():
        data[key] = compute_kpis_for_range(datetime(s.year, s.month, s.day), datetime(e.year, e.month, e.day))

    metrics = ['leads', 'sales', 'churn', 'calls', 'revenue', 'avg_per_sale']
    labels = {'leads': 'Leads', 'sales': 'Sales', 'churn': 'Churn', 'calls': 'Calls',
              'revenue': 'Revenue', 'avg_per_sale': 'Avg $ / Sale'}

    def fd(d):
        return d.strftime('%m/%d')

    def fv(metric, val):
        if val is None:
            return "<span style='color:#ef4444;font-style:italic;'>Couldn't fetch</span>"
        if metric in ('revenue', 'avg_per_sale'):
            return f'${val:,.2f}'
        return str(int(val))

    def pct(curr, prev):
        if curr is None or prev is None:
            return "<span style='color:#94a3b8;'>N/A</span>"
        if prev == 0:
            return '+∞%' if curr > 0 else '0%'
        change = ((curr - prev) / abs(prev)) * 100
        sign = '+' if change >= 0 else ''
        return f'{sign}{change:.1f}%'

    html = f'''<html><body style="font-family:Arial,sans-serif;color:#333;max-width:750px;margin:0 auto;padding:20px;">
<h2 style="color:#6366f1;">Daily Dashboard Performance Report</h2>
<p style="color:#666;">Generated: {today.strftime("%B %d, %Y")} at 8:00 PM PT</p>
<hr style="border:1px solid #e2e8f0;">'''

    for metric in metrics:
        cur7_val = data['cur7'][metric]
        prev7_val = data['prev7'][metric]
        cur_m_val = data['cur_month'][metric]
        prev_m_val = data['prev_month'][metric]
        cur_q_val = data['cur_quarter'][metric]
        prev_q_val = data['prev_quarter'][metric]

        c7 = '#94a3b8' if cur7_val is None or prev7_val is None else ('#10b981' if cur7_val >= prev7_val else '#ef4444')
        cm = '#94a3b8' if cur_m_val is None or prev_m_val is None else ('#10b981' if cur_m_val >= prev_m_val else '#ef4444')
        cq = '#94a3b8' if cur_q_val is None or prev_q_val is None else ('#10b981' if cur_q_val >= prev_q_val else '#ef4444')

        html += f'''<h3 style="color:#1e293b;margin-top:24px;">{labels[metric]}</h3>
<table style="width:100%;border-collapse:collapse;font-size:14px;">
<tr style="background:#f8fafc;">
<td style="padding:8px 12px;border:1px solid #e2e8f0;font-weight:bold;">Current 7 days ({fd(cur7_start)} - {fd(cur7_end)})</td>
<td style="padding:8px 12px;border:1px solid #e2e8f0;font-weight:bold;color:#6366f1;">{fv(metric, cur7_val)}</td>
</tr>
<tr>
<td style="padding:8px 12px;border:1px solid #e2e8f0;">Compared to Previous 7 days ({fd(prev7_start)} - {fd(prev7_end)})</td>
<td style="padding:8px 12px;border:1px solid #e2e8f0;">{fv(metric, prev7_val)} / <span style="color:{c7};font-weight:bold;">{pct(cur7_val, prev7_val)}</span></td>
</tr>
<tr>
<td style="padding:8px 12px;border:1px solid #e2e8f0;">Last 30 Days ({fv(metric, cur_m_val)}) ({fd(cur_month_start)} - {fd(cur_month_end)}) vs Previous 30 Days ({fv(metric, prev_m_val)}) ({fd(prev_month_start)} - {fd(prev_month_end)})</td>
<td style="padding:8px 12px;border:1px solid #e2e8f0;">{fv(metric, prev_m_val)} / <span style="color:{cm};font-weight:bold;">{pct(cur_m_val, prev_m_val)}</span></td>
</tr>
<tr>
<td style="padding:8px 12px;border:1px solid #e2e8f0;">Last 90 Days ({fv(metric, cur_q_val)}) ({fd(cur_q_start)} - {fd(cur_q_end)}) vs Previous 90 Days ({fv(metric, prev_q_val)}) ({fd(prev_q_start)} - {fd(prev_q_end)})</td>
<td style="padding:8px 12px;border:1px solid #e2e8f0;">{fv(metric, prev_q_val)} / <span style="color:{cq};font-weight:bold;">{pct(cur_q_val, prev_q_val)}</span></td>
</tr>
</table>'''

    html += '''<hr style="border:1px solid #e2e8f0;margin-top:30px;">
<p style="color:#94a3b8;font-size:12px;">US Medical Directors — Automated Dashboard Report</p>
</body></html>'''
    return html


def send_report_email():
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return {'success': False, 'error': 'GMAIL_USER and GMAIL_APP_PASSWORD env vars required'}

    today_str = datetime.now(LOCAL_TZ).strftime('%B %d, %Y')
    html_content = generate_report_html()

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'Daily Dashboard Performance Report {today_str}'
    msg['From'] = GMAIL_USER
    msg['To'] = REPORT_TO
    msg['Bcc'] = REPORT_BCC
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            recipients = [REPORT_TO] + [b.strip() for b in REPORT_BCC.split(',')]
            server.sendmail(GMAIL_USER, recipients, msg.as_string())
        return {'success': True, 'message': f'Report sent to {REPORT_TO}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@app.route('/api/email-report', methods=['POST'])
def email_report():
    import threading

    def _run():
        cache.clear()
        send_report_email()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'message': 'Report is being generated and sent in the background.'})


@app.route('/api/email-report/preview')
def email_report_preview():
    try:
        return generate_report_html(), 200, {'Content-Type': 'text/html'}
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# --- Scheduler (APScheduler) ---

def trigger_ar_scrape():
    """Trigger the AR scraper on its Heroku worker dyno via the Heroku API."""
    heroku_api_key = os.environ.get('HEROKU_API_KEY', '')
    heroku_app_name = os.environ.get('AR_SCRAPER_APP', 'scrape-aesthetic-record')
    if not heroku_api_key:
        print('[AR Scraper] No HEROKU_API_KEY set — skipping scrape trigger.')
        return
    try:
        resp = requests.post(
            f'https://api.heroku.com/apps/{heroku_app_name}/dynos',
            headers={
                'Authorization': f'Bearer {heroku_api_key}',
                'Content-Type': 'application/json',
                'Accept': 'application/vnd.heroku+json; version=3',
            },
            json={'command': 'python scrape_AR.py', 'type': 'run'}
        )
        print(f'[AR Scraper] Triggered: {resp.status_code} {resp.text[:200]}')
    except Exception as e:
        print(f'[AR Scraper] Error triggering scrape: {e}')


def init_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            send_report_email,
            CronTrigger(hour=8, minute=0, timezone='America/Los_Angeles'),
            id='daily_email_report',
            replace_existing=True
        )
        scheduler.add_job(
            trigger_ar_scrape,
            IntervalTrigger(days=15),
            id='ar_scrape_15day',
            replace_existing=True
        )
        scheduler.start()
    except ImportError:
        print('APScheduler not installed. Scheduled email report disabled.')


init_scheduler()


# --- MongoDB / Aesthetic Record ---

MONGO_URI = os.environ.get(
    'MONGO_URI',
    'mongodb+srv://Bisma:Bisma123@cluster0.r1tthak.mongodb.net/'
)
mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
ar_db = mongo_client['aesthetic_record']
ar_clients = ar_db['clients']

NOTE_DATE_RE = re.compile(r'Date:\s*(\d{2}/\d{2}/\d{4})')


def parse_procedure_notes(notes_str, start_date=None, end_date=None):
    """Parse procedure_notes string, optionally filtering by date range."""
    if not notes_str:
        return []
    blocks = [b.strip() for b in notes_str.split('||') if b.strip()]
    results = []
    for block in blocks:
        m = NOTE_DATE_RE.search(block)
        if m:
            try:
                note_date = datetime.strptime(m.group(1), '%m/%d/%Y')
            except ValueError:
                continue
            if start_date and note_date < start_date:
                continue
            if end_date and note_date > end_date:
                continue
        elif start_date or end_date:
            continue
        results.append(block)
    return results


@app.route('/api/ar/workspaces')
def ar_get_workspaces():
    try:
        clinics = ar_clients.distinct('clinic_name')
        workspaces = []
        for name in clinics:
            if name:
                count = ar_clients.count_documents({'clinic_name': name})
                workspaces.append({'name': name, 'client_count': count})
        workspaces.sort(key=lambda w: w['name'])
        return jsonify({'success': True, 'workspaces': workspaces})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ar/clients')
def ar_get_clients():
    try:
        start_str = request.args.get('start')
        end_str = request.args.get('end')
        clinic_filter = request.args.get('clinic')
        start_date = datetime.strptime(start_str, '%Y-%m-%d') if start_str else None
        end_date = datetime.strptime(end_str, '%Y-%m-%d').replace(hour=23, minute=59, second=59) if end_str else None

        query = {}
        if clinic_filter:
            query['clinic_name'] = clinic_filter

        docs = list(ar_clients.find(query, {'_id': 0}))

        clients = []
        for doc in docs:
            notes_raw = doc.get('procedure_notes', '')
            filtered_notes = parse_procedure_notes(notes_raw, start_date, end_date)
            if start_date or end_date:
                if not filtered_notes:
                    continue
            name = doc.get('name', '')
            parts = name.strip().split(' ', 1) if name else ['', '']
            first_name = parts[0] if len(parts) > 0 else ''
            last_name = parts[1] if len(parts) > 1 else ''
            client = {
                'scrape_date': doc.get('scrape_date', ''),
                'clinic_name': doc.get('clinic_name', ''),
                'name': name,
                'first_name': first_name,
                'last_name': last_name,
                'client_url': doc.get('client_url', ''),
                'dob': doc.get('dob', ''),
                'age': doc.get('age', ''),
                'address': doc.get('address', ''),
                'email': doc.get('email', ''),
                'phone': doc.get('phone', ''),
                'primary_clinic': doc.get('primary_clinic', ''),
                'creation_date': doc.get('creation_date', ''),
                'customer_notes': doc.get('customer_notes', ''),
                'procedure_notes_count': len(filtered_notes),
                'procedure_notes': filtered_notes,
            }
            clients.append(client)
        return jsonify({'success': True, 'count': len(clients), 'clients': clients})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ar/refresh', methods=['POST'])
def ar_refresh():
    """Placeholder for manual refresh — triggers info message.
    Actual scraping is done by the separate scrape_AR.py project on Heroku."""
    return jsonify({
        'success': True,
        'message': 'Data refresh initiated. The scraper runs on a separate Heroku worker. New data will appear after the next scrape cycle completes.'
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
