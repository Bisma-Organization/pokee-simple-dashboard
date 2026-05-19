from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import os
import requests
import time
from datetime import datetime, timezone
from collections import Counter

app = Flask(__name__, static_folder='static')
CORS(app)

GHL_TOKEN = os.environ.get('GHL_API_TOKEN', '')
GHL_LOCATION = os.environ.get('GHL_LOCATION_ID', '')
MONDAY_TOKEN = os.environ.get('MONDAY_API_TOKEN', '')

GHL_BASE = 'https://services.leadconnectorhq.com'
MONDAY_BASE = 'https://api.monday.com/v2'

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
    start_str = request.args.get('start')
    end_str = request.args.get('end')
    start = datetime.fromisoformat(start_str).replace(tzinfo=timezone.utc) if start_str else None
    end = datetime.fromisoformat(end_str).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc) if end_str else None
    return start, end


def fetch_all_contacts():
    def _fetch():
        all_contacts = []
        params = {'locationId': GHL_LOCATION, 'limit': 100, 'sortBy': 'date_added', 'order': 'desc'}
        for _ in range(20):
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
            records.append({
                'name': item['name'],
                'status': status,
                'start_date': start_date,
                'end_date': end_date,
                'due_date': due_date,
                'ar': ar
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


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/kpis')
def get_kpis():
    start, end = get_date_range()

    contacts = fetch_all_contacts()
    leads = [c for c in contacts if in_range(parse_date(c.get('dateAdded')), start, end)]

    sales_data = fetch_sales_data()
    sales = [s for s in sales_data if in_range(parse_date(s.get('first_payment')), start, end)]
    total_revenue = sum(s['fee'] for s in sales)

    churn_data = fetch_churn_data()
    churn = [c for c in churn_data if in_range(parse_date(c.get('end_date')), start, end)]

    convos = fetch_conversations()
    calls = [c for c in convos if in_range(parse_date(c.get('dateAdded')), start, end)]

    payment_data = fetch_payment_data()
    active_clients = [p for p in payment_data if p.get('status') == 'Active']

    sales_count = len(sales)
    avg_per_sale = total_revenue / sales_count if sales_count > 0 else 0

    return jsonify({
        'leads': len(leads),
        'sales': sales_count,
        'churn': len(churn),
        'calls': len(calls),
        'revenue': round(total_revenue, 2),
        'avg_per_sale': round(avg_per_sale, 2),
        'active_clients': len(active_clients),
        'total_contacts': len(contacts),
        'total_conversations': len(convos)
    })


@app.route('/api/leads')
def get_leads():
    start, end = get_date_range()
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))

    contacts = fetch_all_contacts()
    filtered = [c for c in contacts if in_range(parse_date(c.get('dateAdded')), start, end)]
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
    start, end = get_date_range()
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))

    convos = fetch_conversations()
    filtered = [c for c in convos if in_range(parse_date(c.get('dateAdded')), start, end)]
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

    return jsonify({'rows': rows, 'total': total, 'page': page, 'pages': (total + limit - 1) // limit})


@app.route('/api/revenue')
def get_revenue():
    start, end = get_date_range()
    sales_data = fetch_sales_data()
    filtered = [s for s in sales_data if in_range(parse_date(s.get('first_payment')), start, end)]

    monthly = {}
    for s in filtered:
        dt = parse_date(s['first_payment'])
        if dt:
            key = dt.strftime('%Y-%m')
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
            'ar': p.get('ar', '')
        })

    status_counts = Counter(p.get('status', 'Unknown') for p in payment_data)
    return jsonify({'rows': rows, 'total': len(rows), 'by_status': dict(status_counts)})


@app.route('/api/pipeline')
def get_pipeline():
    data = fetch_pipeline_data()
    return jsonify({'stages': data})


@app.route('/api/leads-monthly')
def get_leads_monthly():
    start, end = get_date_range()
    contacts = fetch_all_contacts()
    filtered = [c for c in contacts if in_range(parse_date(c.get('dateAdded')), start, end)]

    monthly = {}
    for c in filtered:
        dt = parse_date(c.get('dateAdded'))
        if dt:
            key = dt.strftime('%Y-%m')
            monthly[key] = monthly.get(key, 0) + 1

    sorted_months = sorted(monthly.items())
    return jsonify({'monthly': [{'month': m, 'count': v} for m, v in sorted_months]})


@app.route('/api/refresh', methods=['POST'])
def refresh_cache():
    cache.clear()
    return jsonify({'status': 'ok', 'message': 'Cache cleared'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
