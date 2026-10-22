"""
Tabbycat Draw Viewer v1.0  (free Render web service)

Shows the draw of one round like the tab site's big-screen draw page (venue, teams, adjudicators, auto-scroll, text size,
hide adjudicators) with the SPEAKERS' NAMES under every team name.  READ-ONLY: it only reads from Tabbycat's API.

Data it reads (GET only)
  /api/v1/tournaments/<slug>                       tournament name
  /api/v1/tournaments/<slug>/rounds                the round, its start time and draw status
  /api/v1/tournaments/<slug>/rounds/<n>/pairings   the debates: room, teams (side) and adjudicators
  /api/v1/tournaments/<slug>/teams                 team names (code name / short / long / reference) and speakers
  /api/v1/tournaments/<slug>/adjudicators          adjudicator names
  /api/v1/tournaments/<slug>/venues                room names
A released draw is public, so the token is optional for it; an admin token also shows an unreleased (draft/confirmed) draw.
"""
import re
from datetime import datetime

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)


# =============================================================================
# TABBYCAT API READER (GET only) - the same connection code as the exporter tools
# =============================================================================

class ApiError(Exception):
    def __init__(self, status, url, body=''):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} on {url}")

    def friendly(self):
        short = self.url
        if '/api/' in short:
            short = '/api/' + short.split('/api/', 1)[1]
        if self.status == 401:
            return (f"401 Unauthorized on {short} - the API token is missing/invalid. "
                    f"Use a token from an ADMIN account (Change Password page).")
        if self.status == 403:
            return (f"403 Forbidden on {short} - this account has no access to that data. "
                    f"Feedback and pairings need tab/adjudication-core (admin) access.")
        if self.status == 404:
            return f"404 Not Found on {short} - check the URL and tournament slug."
        return f"HTTP {self.status} on {short}: {self.body[:150]}"


class TabbycatReader:
    def __init__(self, base_url, token, tournament_slug, username=None, password=None):
        self.base_url = base_url.strip().rstrip('/')
        self.token = (token or '').strip()
        self.slug = (tournament_slug or '').strip().strip('/')
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'TabbycatExporter/1.0 (Render; Python requests)',
            'Accept': 'application/json',
        })
        self.auth_method = None
        self._authenticate()

    # ---- URLs --------------------------------------------------------------
    def global_url(self, path):
        return f"{self.base_url}/api/v1{path}"

    def tournament_url(self, path=''):
        return f"{self.base_url}/api/v1/tournaments/{self.slug}{path}"

    # ---- authentication (same idea as the importer) -------------------------
    def _authenticate(self):
        probe = self.tournament_url('/adjudicators')
        if self.token:
            self.session.headers['Authorization'] = f'Token {self.token}'
            if self._status(probe) == 200:
                self.auth_method = 'token'
                return

        if self.username and self.password:
            self.session.headers.pop('Authorization', None)
            if self._login_session() and self._status(probe) == 200:
                self.auth_method = 'session'
                return

        # Keep the token header so later errors show the real status code
        if self.token:
            self.session.headers['Authorization'] = f'Token {self.token}'

    def _status(self, url):
        try:
            return self.session.get(url, timeout=20).status_code
        except requests.exceptions.RequestException:
            return 0

    def _login_session(self):
        try:
            login_url = f"{self.base_url}/accounts/login/"
            resp = self.session.get(login_url, timeout=15)
            match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', resp.text)
            csrf = match.group(1) if match else ''
            self.session.post(login_url, data={
                'username': self.username,
                'password': self.password,
                'csrfmiddlewaretoken': csrf,
                'next': '/',
            }, headers={'Referer': login_url}, timeout=15)
            return self.session.get(f"{self.base_url}/database/", timeout=15).status_code == 200
        except Exception:
            return False

    # ---- reading ------------------------------------------------------------
    def get_json(self, url, retries=3):
        last_error = None
        for attempt in range(retries):
            try:
                resp = self.session.get(url, timeout=60)
            except requests.exceptions.RequestException as exc:
                last_error = exc
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise ApiError(resp.status_code, url, 'Response was not JSON')
            if resp.status_code == 429:
                import time
                time.sleep(2 ** attempt)
                continue
            raise ApiError(resp.status_code, url, resp.text)
        raise ApiError(0, url, str(last_error) if last_error else 'Request failed')

    def get_list(self, url):
        """GET a list endpoint; tolerates {'results': [...], 'next': url} pagination."""
        data = self.get_json(url)
        items = []
        guard = 0
        while True:
            if isinstance(data, list):
                items.extend(data)
                break
            if isinstance(data, dict) and isinstance(data.get('results'), list):
                items.extend(data['results'])
                nxt = data.get('next')
                guard += 1
                if nxt and guard < 200:
                    data = self.get_json(nxt)
                    continue
            break
        return items

# =============================================================================
# DRAW DATA
# =============================================================================

NAME_FIELDS = {
    'code': ('code_name', 'short_name', 'long_name', 'reference'),
    'short': ('short_name', 'long_name', 'reference', 'code_name'),
    'long': ('long_name', 'short_name', 'reference', 'code_name'),
    'reference': ('reference', 'long_name', 'short_name', 'code_name'),
}
BP_SIDES = ['og', 'oo', 'cg', 'co']
SIDE_LABELS = {'og': 'Opening Government', 'oo': 'Opening Opposition', 'cg': 'Closing Government',
               'co': 'Closing Opposition', 'aff': 'Affirmative', 'neg': 'Negative'}


def url_id(value):
    """Last number of an API hyperlink (or a plain id)."""
    if isinstance(value, dict):
        value = value.get('url') or value.get('id')
    text = str(value or '').rstrip('/')
    m = re.search(r'(\d+)$', text)
    return int(m.group(1)) if m else None


def key_of(value):
    return str(url_id(value))


def team_name(team, field):
    for f in NAME_FIELDS.get(field, NAME_FIELDS['code']):
        if team.get(f):
            return str(team[f])
    return f"Team {team.get('id', '?')}"


def side_of(entry, position):
    """-> 'og' / 'oo' / 'cg' / 'co' / 'aff' / 'neg' / None for one entry of a pairing's teams list."""
    side = entry.get('side') if isinstance(entry, dict) else None
    if isinstance(side, str) and side.lower() in SIDE_LABELS:
        return side.lower()
    if isinstance(side, int):
        return BP_SIDES[side] if 0 <= side < 4 else None
    return None


def round_start_text(rnd, tz_name):
    stamp = rnd.get('starts_at')
    if not stamp:
        return ''
    try:
        when = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
        try:
            from zoneinfo import ZoneInfo
            when = when.astimezone(ZoneInfo(tz_name))
        except Exception:
            tz_name = 'UTC' if when.tzinfo else tz_name
        return f"{when.strftime('%H:%M')} (in {tz_name})"
    except ValueError:
        return ''


def build_draw(reader, round_text, name_field='code', tz_name='Asia/Manila'):
    tournament = reader.get_json(reader.tournament_url())
    rounds = reader.get_list(reader.tournament_url('/rounds'))
    wanted = str(round_text).strip().lower()
    rnd = next((r for r in rounds if str(r.get('seq')) == wanted), None) \
        or next((r for r in rounds if str(r.get('abbreviation', '')).lower() == wanted
                 or str(r.get('name', '')).lower() == wanted), None)
    if rnd is None:
        raise ValueError(f"Round '{round_text}' not found. Rounds on this tab: "
                         + ', '.join(str(r.get('seq')) for r in rounds))
    if rnd.get('draw_status') == 'N':
        raise ValueError(f"{rnd.get('name') or 'This round'} has no draw yet.")
    pairing_url = ((rnd.get('_links') or {}).get('pairing')) or (str(rnd.get('url', '')).rstrip('/') + '/pairings')
    pairings = reader.get_list(pairing_url)

    teams = {key_of(t): t for t in reader.get_list(reader.tournament_url('/teams'))}
    speakers_by_team = {}
    for tid, t in teams.items():
        names = [s.get('name') for s in (t.get('speakers') or []) if isinstance(s, dict) and s.get('name')]
        if names:
            speakers_by_team[tid] = names
    if teams and len(speakers_by_team) < len(teams) // 2:            # team list had no speaker names: use /speakers
        try:
            speakers_by_team = {}
            for s in reader.get_list(reader.tournament_url('/speakers')):
                if s.get('name') and s.get('team'):
                    speakers_by_team.setdefault(key_of(s['team']), []).append(s['name'])
        except ApiError:
            pass

    adjs = {key_of(a): a.get('name', '') for a in reader.get_list(reader.tournament_url('/adjudicators'))}
    venues = {}
    try:
        for v in reader.get_list(reader.tournament_url('/venues')):
            venues[key_of(v)] = v.get('display_name') or v.get('name') or ''
    except ApiError:
        pass

    def sort_key(p):
        return (p.get('room_rank') if isinstance(p.get('room_rank'), (int, float)) else 10 ** 6,
                -(p.get('bracket') or 0), p.get('id') or 0)

    rows, layout = [], None
    for p in sorted(pairings, key=sort_key):
        slots = []
        entries = p.get('teams') or []
        for position, entry in enumerate(entries):
            side = side_of(entry, position)
            ref = entry.get('team') if isinstance(entry, dict) else entry
            team = teams.get(key_of(ref), {})
            slots.append({'side': side, 'name': team_name(team, name_field) if team else '(unknown team)',
                          'emoji': team.get('emoji') or '', 'speakers': speakers_by_team.get(key_of(ref), [])})
        adj = p.get('adjudicators') or {}
        if isinstance(adj, list):
            adj = {'chair': adj[0] if adj else None, 'panellists': adj[1:], 'trainees': []}
        rows.append({
            'venue': venues.get(key_of(p.get('room')), '') if p.get('room') else '',
            'teams': slots,
            'chair': adjs.get(key_of(adj.get('chair')), '') if adj.get('chair') else '',
            'panellists': [adjs.get(key_of(u), '') for u in (adj.get('panellists') or [])],
            'trainees': [adjs.get(key_of(u), '') for u in (adj.get('trainees') or [])],
        })

    # column headings: BP sides if the pairings say so (or 4 teams), otherwise the sides as given, in order
    sides = [s['side'] for r in rows[:1] for s in r['teams']]
    if rows and all(sides) and len(set(sides)) == len(sides):
        order = [x for x in BP_SIDES + ['aff', 'neg'] if x in sides]
        for r in rows:                                   # put every debate's teams in the same column order
            r['teams'].sort(key=lambda s: order.index(s['side']) if s['side'] in order else 99)
        layout = [SIDE_LABELS[x] for x in order]
    else:
        count = max((len(r['teams']) for r in rows), default=0)
        layout = ([SIDE_LABELS[x] for x in BP_SIDES] if count == 4 else
                  ['Affirmative', 'Negative'] if count == 2 else [f"Team {i + 1}" for i in range(count)])
    start = round_start_text(rnd, tz_name)
    round_name = rnd.get('name') or f"Round {rnd.get('seq')}"
    return {
        'tournament': tournament.get('name') or reader.slug,
        'round_name': round_name,
        'banner': f"Draw for {round_name} debates start at {start}" if start else f"Draw for {round_name}",
        'draw_status': rnd.get('draw_status'),
        'positions': layout,
        'rows': rows,
    }


# =============================================================================
# ROUTES
# =============================================================================

def reader_from_form(data):
    return TabbycatReader(data.get('base_url', ''), data.get('token', ''), data.get('slug', ''),
                          username=data.get('username') or None, password=data.get('password') or None)


def load(data):
    reader = reader_from_form(data)
    return build_draw(reader, data.get('round') or '1', data.get('name_field') or 'code',
                      (data.get('timezone') or 'Asia/Manila').strip())


def explain(exc):
    if isinstance(exc, ApiError):
        return exc.friendly()
    return str(exc)


@app.route('/')
def index():
    return render_template('index.html', error=None, f={})


@app.route('/draw', methods=['POST'])
def draw_page():
    f = request.form
    data = {'base_url': f.get('api_url', ''), 'token': f.get('api_token', ''), 'slug': f.get('tournament_slug', ''),
            'username': f.get('api_username', ''), 'password': f.get('api_password', ''), 'round': f.get('round', '1'),
            'name_field': f.get('name_field', 'code'), 'timezone': f.get('timezone', 'Asia/Manila')}
    if not data['base_url'].strip() or not data['slug'].strip():
        return render_template('index.html', error='Enter the Tabbycat URL and the tournament slug.', f=f)
    try:
        draw = load(data)
    except Exception as exc:
        return render_template('index.html', error=explain(exc), f=f)
    show_speakers = f.get('show_speakers') == 'on'
    return render_template('draw.html', draw=draw, creds=data, show_speakers=show_speakers,
                           show_adjs=f.get('show_adjudicators') == 'on')


@app.route('/data', methods=['POST'])
def data_route():
    try:
        return jsonify({'ok': True, 'draw': load(request.get_json(force=True) or {})})
    except Exception as exc:
        return jsonify({'ok': False, 'error': explain(exc)}), 200


if __name__ == '__main__':
    import os
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
