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
    end = datetime.fromisoformat(end_str).replace(tzinfo=timezone.utc) if end_str else None
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


def fetch_revenue_data():
    def _fetch():
        query = '''{ boards(ids: [1944309746]) { items_page(limit: 500,
            query_params: {order_by: [{column_id: "__last_updated__", direction: desc}]})
            { items { id name column_values(ids: ["numbers_Mjivm65q", "date_Mjiv0T8Z", "status"])
            { id text } } } } }'''
        resp = monday_query(query)
        items = resp['data']['boards'][0]['items_page']['items']
        records = []
        for item in items:
            fee = 0
            payment_date = None
            status = ''
            for cv in item['column_values']:
                if cv['id'] == 'numbers_Mjivm65q':
                    try:
                        fee = float(cv['text']) if cv['text'] else 0
                    except ValueError:
                        fee = 0
                elif cv['id'] == 'date_Mjiv0T8Z':
                    payment_date = cv['text']
                elif cv['id'] == 'status':
                    status = cv['text'] or ''
            if fee > 0 and payment_date:
                records.append({
                    'name': item['name'],
                    'fee': fee,
                    'date': payment_date,
                    'status': status
                })
        return records
    return cached('revenue', _fetch)


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

    won = fetch_opportunities('won')
    sales = [o for o in won if in_range(parse_date(o.get('lastStatusChangeAt')), start, end)]

    lost = fetch_opportunities('lost')
    churn = [o for o in lost if in_range(parse_date(o.get('lastStatusChangeAt')), start, end)]

    convos = fetch_conversations()
    calls = [c for c in convos if in_range(parse_date(c.get('dateAdded')), start, end)]

    rev_data = fetch_revenue_data()
    revenue_items = [r for r in rev_data if in_range(parse_date(r['date']), start, end)]
    total_revenue = sum(r['fee'] for r in revenue_items)

    sales_count = len(sales) if sales else len(revenue_items)
    avg_per_sale = total_revenue / sales_count if sales_count > 0 else 0

    return jsonify({
        'leads': len(leads),
        'sales': len(sales),
        'churn': len(churn),
        'calls': len(calls),
        'revenue': round(total_revenue, 2),
        'avg_per_sale': round(avg_per_sale, 2),
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
    won = fetch_opportunities('won')
    filtered = [o for o in won if in_range(parse_date(o.get('lastStatusChangeAt')), start, end)]
    filtered.sort(key=lambda o: o.get('lastStatusChangeAt', ''), reverse=True)

    rows = []
    for o in filtered:
        rows.append({
            'name': o.get('name', ''),
            'value': o.get('monetaryValue', 0),
            'source': o.get('source', ''),
            'date': o.get('lastStatusChangeAt', ''),
            'created': o.get('createdAt', ''),
            'email': o.get('contact', {}).get('email', ''),
            'phone': o.get('contact', {}).get('phone', '')
        })

    return jsonify({'rows': rows, 'total': len(rows)})


@app.route('/api/churn')
def get_churn():
    start, end = get_date_range()
    lost = fetch_opportunities('lost')
    filtered = [o for o in lost if in_range(parse_date(o.get('lastStatusChangeAt')), start, end)]
    filtered.sort(key=lambda o: o.get('lastStatusChangeAt', ''), reverse=True)

    rows = []
    for o in filtered:
        rows.append({
            'name': o.get('name', ''),
            'source': o.get('source', ''),
            'date': o.get('lastStatusChangeAt', ''),
            'email': o.get('contact', {}).get('email', ''),
            'phone': o.get('contact', {}).get('phone', '')
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
    rev_data = fetch_revenue_data()
    filtered = [r for r in rev_data if in_range(parse_date(r['date']), start, end)]

    monthly = {}
    for r in filtered:
        dt = parse_date(r['date'])
        if dt:
            key = dt.strftime('%Y-%m')
            monthly[key] = monthly.get(key, 0) + r['fee']

    sorted_months = sorted(monthly.items())

    return jsonify({
        'rows': filtered,
        'monthly': [{'month': m, 'revenue': round(v, 2)} for m, v in sorted_months],
        'total': round(sum(r['fee'] for r in filtered), 2)
    })


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
