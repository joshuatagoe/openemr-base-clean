// k6 script for the agent briefing path (ARCHITECTURE.md section 13).
// Same sequence as loadtest/run_load.py: signed bundle post -> ticket -> SSE briefing -> delete.
//
//   k6 run -e BASE_URL=http://127.0.0.1:8766 -e SECRET=<COPILOT_TICKET_SECRET> --vus 10 --iterations 50 loadtest/k6_briefing.js
//
// Pass criteria (section 13): 10 VUs p95 <= 8 s and errors < 1 %; 50 VUs no timeouts without a
// degraded frame and errors < 5 %.

import http from 'k6/http';
import { check } from 'k6';
import crypto from 'k6/crypto';
import encoding from 'k6/encoding';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8766';
const SECRET = __ENV.SECRET;
const FIXTURE = JSON.parse(open('../fixtures/lab_followup.json')).context;

export const options = {
    thresholds: {
        'http_req_failed': ['rate<0.01'],
        'briefing_duration': ['p(95)<8000'],
        'briefing_degraded': ['count==0'],
    },
};

import { Trend, Counter } from 'k6/metrics';
const briefingDuration = new Trend('briefing_duration', true);
const briefingDegraded = new Counter('briefing_degraded');

function b64url(input) {
    return encoding.b64encode(input, 'rawurl');
}

function mintTicket(claims) {
    const header = b64url(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
    const payload = b64url(JSON.stringify(claims));
    const sig = crypto.hmac('sha256', SECRET, `${header}.${payload}`, 'binary');
    return `${header}.${payload}.${b64url(sig)}`;
}

export default function () {
    const ctx = Object.assign({}, FIXTURE, { correlation_id: uuidv4(), patient_uuid: uuidv4() });
    const body = JSON.stringify(ctx);
    const ts = Math.floor(Date.now() / 1000);
    const sig = 'v1=' + crypto.hmac('sha256', SECRET, `${ts}.${body}`, 'hex');
    const started = Date.now();
    const post = http.post(`${BASE}/v1/bundles`, body, { headers: { 'Content-Type': 'application/json', 'X-Copilot-Timestamp': String(ts), 'X-Copilot-Signature': sig } });
    if (!check(post, { 'bundle accepted': (r) => r.status === 201 })) {
        return;
    }
    const acc = post.json();
    const now = Math.floor(Date.now() / 1000);
    const token = mintTicket({ sub: uuidv4(), puuid: acc.patient_uuid, bundle_id: acc.bundle_id, cid: acc.correlation_id, jti: uuidv4(), iat: now, exp: now + 120 });
    const stream = http.get(`${BASE}/v1/briefings/${acc.bundle_id}`, { headers: { Authorization: `Bearer ${token}` }, timeout: '60s' });
    const ok = check(stream, { 'stream 200': (r) => r.status === 200, 'terminal frame': (r) => r.body.includes('event: complete') || r.body.includes('event: degraded') });
    if (ok && stream.body.includes('event: degraded')) {
        briefingDegraded.add(1);
    }
    briefingDuration.add(Date.now() - started);
    http.del(`${BASE}/v1/bundles/${acc.bundle_id}`, null, { headers: { Authorization: `Bearer ${token}` } });
}
