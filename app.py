"""
Tabbycat Draw Viewer v1.1  (free Render web service, public link)

Shows the released draw of a round in the look of the tab site's own draw page (OG/OO/CG/CO columns, team emojis, adjudicators
with (C)/(T), Find in Table, text size, hide adjudicators, slow auto-scroll) with the SPEAKERS' NAMES under every team name.
READ-ONLY: it only reads from Tabbycat's API.

Set it up once on Render with environment variables (so the link can be opened by anyone, with nothing to type):
  TAB_URL     https://ndc2026.calicotab.com        TAB_SLUG   ndc2026
  TAB_TOKEN   (optional) admin token - needed if the tab keeps adjudicator names / pairings private; stays on the server
  TEAM_NAME   code (default) | short | long | reference      TAB_TIMEZONE   Asia/Manila (default)
  ALLOW_UNRELEASED   1 = also show draws that are not released yet (default: only released draws are shown)
Without TAB_URL the page opens a small setup form, and the link can carry  ?url=...&slug=...  instead.

Data it reads (GET only)
  /api/v1/tournaments/<slug>                       tournament name
  /api/v1/tournaments/<slug>/rounds                the round, its start time and draw status
  /api/v1/tournaments/<slug>/rounds/<n>/pairings   the debates: room, teams (side) and adjudicators
  /api/v1/tournaments/<slug>/teams                 team names (code name / short / long / reference) and speakers
  /api/v1/tournaments/<slug>/adjudicators          adjudicator names
  /api/v1/tournaments/<slug>/venues                room names
A released draw is public, so the token is optional for it; an admin token also shows an unreleased (draft/confirmed) draw.
"""
import ipaddress
import os
import re
import time
from datetime import datetime
from urllib.parse import urlencode, urlsplit

import requests
from flask import Flask, jsonify, redirect, render_template, request

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
SIDE_LABELS = {'og': 'OG', 'oo': 'OO', 'cg': 'CG', 'co': 'CO', 'aff': 'Aff', 'neg': 'Neg'}
CACHE_SECONDS = 30
CACHE = {}


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


def side_of(entry):
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


def find_round(rounds, wanted):
    wanted = str(wanted).strip().lower()
    return next((r for r in rounds if str(r.get('seq')) == wanted), None) \
        or next((r for r in rounds if str(r.get('abbreviation', '')).lower() == wanted
                 or str(r.get('name', '')).lower() == wanted), None)


def build_draw(reader, round_text, name_field='code', tz_name='Asia/Manila', allow_unreleased=False):
    tournament = reader.get_json(reader.tournament_url())
    rounds = reader.get_list(reader.tournament_url('/rounds'))
    rnd = find_round(rounds, round_text)
    if rnd is None:
        raise ValueError(f"Round '{round_text}' not found. Rounds on this tab: "
                         + ', '.join(str(r.get('seq')) for r in rounds))
    status = rnd.get('draw_status')
    if status == 'N':
        raise ValueError(f"{rnd.get('name') or 'This round'} has no draw yet.")
    if status != 'R' and not allow_unreleased:
        raise ValueError(f"The draw for {rnd.get('name') or 'this round'} has not been released yet.")
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

    adjs, adjs_hidden = {}, False
    try:
        adjs = {key_of(a): a.get('name', '') for a in reader.get_list(reader.tournament_url('/adjudicators'))}
    except ApiError:
        adjs_hidden = True                                              # the tab keeps adjudicators private (needs a token)
    venues = {}
    try:
        for v in reader.get_list(reader.tournament_url('/venues')):
            venues[key_of(v)] = v.get('display_name') or v.get('name') or ''
    except ApiError:
        pass

    def sort_key(p):
        return (p.get('room_rank') if isinstance(p.get('room_rank'), (int, float)) else 10 ** 6,
                -(p.get('bracket') or 0), p.get('id') or 0)

    rows = []
    for p in sorted(pairings, key=sort_key):
        slots = []
        for entry in (p.get('teams') or []):
            ref = entry.get('team') if isinstance(entry, dict) else entry
            team = teams.get(key_of(ref), {})
            slots.append({'side': side_of(entry), 'name': team_name(team, name_field) if team else '(unknown team)',
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

    # column headings: OG/OO/CG/CO when the pairings say so (or 4 teams), otherwise Aff/Neg or Team 1, 2 ...
    sides = [s['side'] for r in rows[:1] for s in r['teams']]
    if rows and all(sides) and len(set(sides)) == len(sides):
        order = [x for x in BP_SIDES + ['aff', 'neg'] if x in sides]
        for r in rows:                                   # every debate's teams in the same column order
            r['teams'].sort(key=lambda s: order.index(s['side']) if s['side'] in order else 99)
        layout = [SIDE_LABELS[x] for x in order]
    else:
        count = max((len(r['teams']) for r in rows), default=0)
        layout = (['OG', 'OO', 'CG', 'CO'] if count == 4 else ['Aff', 'Neg'] if count == 2
                  else [f"Team {i + 1}" for i in range(count)])
    start = round_start_text(rnd, tz_name)
    round_name = rnd.get('name') or f"Round {rnd.get('seq')}"
    shown = [{'seq': r.get('seq'), 'name': r.get('name') or f"Round {r.get('seq')}"} for r in rounds
             if r.get('draw_status') == 'R' or (allow_unreleased and r.get('draw_status') not in (None, 'N'))]
    return {
        'tournament': tournament.get('name') or reader.slug,
        'round_name': round_name,
        'round_seq': rnd.get('seq'),
        'start': start,
        'positions': layout,
        'rows': rows,
        'draw_rounds': shown,
        'all_rounds': [{'seq': r.get('seq'), 'name': r.get('name') or f"Round {r.get('seq')}"} for r in rounds],
        'adjs_hidden': adjs_hidden,
    }


# =============================================================================
# CONFIGURATION (environment variables, or ?url=...&slug=... in the link)
# =============================================================================

def safe_url(url):
    """The viewer is public: only plain https tab sites by host name (no IP addresses / localhost) can be read."""
    parts = urlsplit((url or '').strip())
    host = (parts.hostname or '').lower()
    if parts.scheme != 'https' or not host or host == 'localhost' or '.' not in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def get_config():
    env_url, env_slug = os.environ.get('TAB_URL', '').strip(), os.environ.get('TAB_SLUG', '').strip()
    locked = bool(env_url and env_slug)
    q = request.args
    cfg = {
        'url': (env_url if locked else q.get('url', '')).strip().rstrip('/'),
        'slug': (env_slug if locked else q.get('slug', '')).strip().strip('/'),
        'name': (q.get('name') or os.environ.get('TEAM_NAME') or 'code').strip().lower(),
        'tz': (q.get('tz') or os.environ.get('TAB_TIMEZONE') or 'Asia/Manila').strip(),
        'token': os.environ.get('TAB_TOKEN', '').strip() if locked else '',
        'allow_unreleased': os.environ.get('ALLOW_UNRELEASED', '') == '1',
        'locked': locked,
    }
    cfg['ok'] = bool(cfg['url'] and cfg['slug'] and (locked or safe_url(cfg['url'])))
    return cfg


def query_string(cfg):
    """Keep the link's own settings when moving between rounds (nothing when the server holds them)."""
    if cfg['locked']:
        return ''
    return '?' + urlencode({'url': cfg['url'], 'slug': cfg['slug'], 'name': cfg['name'], 'tz': cfg['tz']})


def load_draw(cfg, round_text):
    key = (cfg['url'], cfg['slug'], str(round_text), cfg['name'], cfg['tz'], cfg['allow_unreleased'])
    hit = CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    reader = TabbycatReader(cfg['url'], cfg['token'], cfg['slug'])
    draw = build_draw(reader, round_text, cfg['name'], cfg['tz'], cfg['allow_unreleased'])
    CACHE[key] = (time.time(), draw)
    if len(CACHE) > 60:
        CACHE.pop(min(CACHE, key=lambda k: CACHE[k][0]), None)
    return draw


def explain(exc):
    if isinstance(exc, ApiError):
        return exc.friendly()
    return str(exc)


# =============================================================================
# ROUTES
# =============================================================================

@app.route('/')
def index():
    cfg = get_config()
    if not cfg['ok']:
        error = 'That tab site address is not allowed (use its https:// address).' if request.args.get('url') else None
        return render_template('index.html', error=error, f=request.args)
    try:
        reader = TabbycatReader(cfg['url'], cfg['token'], cfg['slug'])
        rounds = reader.get_list(reader.tournament_url('/rounds'))
    except Exception as exc:
        return render_template('index.html', error=explain(exc), f=request.args)
    ok = [r for r in rounds if r.get('draw_status') == 'R' or (cfg['allow_unreleased'] and r.get('draw_status') not in (None, 'N'))]
    if not ok:
        return render_template('index.html', error='No draw has been released on this tab yet.', f=request.args)
    latest = max(ok, key=lambda r: r.get('seq') or 0)
    return redirect(f"/round/{latest.get('seq')}{query_string(cfg)}")


@app.route('/round/<round_text>')
def draw_page(round_text):
    cfg = get_config()
    if not cfg['ok']:
        return redirect('/')
    try:
        draw = load_draw(cfg, round_text)
    except Exception as exc:
        return render_template('index.html', error=explain(exc), f=request.args), 200
    return render_template('draw.html', draw=draw, qs=query_string(cfg), site=f"{cfg['url']}/{cfg['slug']}")


@app.route('/data/<round_text>')
def data_route(round_text):
    cfg = get_config()
    if not cfg['ok']:
        return jsonify({'ok': False, 'error': 'Not configured.'})
    try:
        return jsonify({'ok': True, 'draw': load_draw(cfg, round_text)})
    except Exception as exc:
        return jsonify({'ok': False, 'error': explain(exc)})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
