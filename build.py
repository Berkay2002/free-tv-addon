"""Build a static Stremio Live TV addon from iptv-org + Free-TV.

Merges both sources, keeps Swedish/Turkish/English channels, health-checks
every stream, dedupes channels, adds a week of programme guide and writes a
static addon into ./site.
Stdlib only. Run: python build.py   (python build.py --test for the self-check)
"""
import concurrent.futures as cf
import gzip
import io
import json
import re
import shutil
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

LANGS = {'swe': 'Swedish', 'tur': 'Turkish', 'eng': 'English'}
ENGLISH_COUNTRIES = {'US', 'UK', 'GB', 'CA', 'AU', 'IE', 'NZ'}
FREETV_GROUPS = {'Sweden': 'swe', 'Turkey': 'tur', 'UK': 'eng', 'USA': 'eng', 'Canada': 'eng',
                 'Australia': 'eng', 'Ireland': 'eng', 'New Zealand': 'eng'}
SKIP_CATEGORIES = {'religious', 'shop', 'interactive', 'xxx', 'legislative'}
ENGLISH_GROUPS = [  # first match wins
    ('English Kids', {'kids', 'animation', 'family'}),
    ('English News', {'news', 'weather', 'business'}),
    ('English Sports', {'sports', 'outdoor', 'auto'}),
    ('English Movies', {'movies', 'classic'}),
    ('English Music', {'music'}),
    ('English Docs & Lifestyle', {'documentary', 'science', 'education', 'culture', 'travel',
                                  'cooking', 'lifestyle', 'relax', 'public'}),
]
GROUP_ORDER = ['Swedish', 'Turkish'] + [g for g, _ in ENGLISH_GROUPS] + ['English Entertainment']
# Channels listed first (and so numbered first) in their group, matched on name.
PRIORITY = {
    'Swedish': ['SVT1', 'SVT2', 'SVT Barn', 'Kunskapskanalen', 'SVT24', 'TV4', 'Sjuan', 'TV12'],
    'Turkish': ['TRT 1', 'TRT Haber', 'TRT Spor', 'TRT Çocuk', 'TRT Belgesel', 'TRT Müzik'],
    'English News': ['BBC News', 'Sky News', 'CNN', 'Bloomberg TV', 'NBC News NOW', 'CBS News 24/7',
                     'ABC News Live', 'Al Jazeera English', 'France 24 English', 'DW English'],
    'English Entertainment': ['BBC One', 'BBC Two'],
}
MAX_SOURCES = 3
UA = 'VLC/3.0.21 LibVLC/3.0.21'
API = 'https://iptv-org.github.io/api/'
FREETV = 'https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8'
CTX = ssl.create_default_context()
# Programme guides (XMLTV, gzip) and the country their channel names are in. Pluto TV's ids are
# in its stream URLs, so it matches by id alone; epgshare01's ids end in their country.
EPG_FEEDS = [('https://i.mjh.nz/PlutoTV/all.xml.gz', None), ('https://i.mjh.nz/SamsungTVPlus/us.xml.gz', 'us'),
             ('https://i.mjh.nz/Plex/us.xml.gz', 'us'), ('https://i.mjh.nz/Roku/all.xml.gz', 'us')] + [
    (f'https://epgshare01.online/epgshare01/epg_ripper_{c}.xml.gz', None)
    for c in ('SE1', 'TR1', 'TR3', 'UK1', 'US2', 'CA2', 'AU1', 'IE1', 'NZ1')]
DAY = 86400
GUIDE_DAYS = range(0, 7)  # UTC days: today through today+6 (no catch-up, so no yesterday)
BLOCK = 3 * 3600  # placeholder programme length, aligned to UTC
MIN_GAP = 300  # gaps shorter than this stretch the programme before them
MAX_TITLE, MAX_DESC = 200, 500
MAX_FEED, MAX_XML = 100 << 20, 400 << 20  # bytes downloaded / decompressed per feed


def fetch(url, timeout=15, limit=None, headers=None):
    # Playlist URLs are untrusted: never let them reach file:// or ftp://.
    # ponytail: no private-IP blocking; bodies are never published, only alive/dead.
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError(f'unsupported scheme: {url}')
    req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.status, r.geturl(), r.headers.get('Content-Type', ''), r.read(limit)


def parse_m3u(text):
    """[(attrs, name, url)] from an extended M3U."""
    out, attrs, name = [], None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF'):
            head, _, name = line.partition(',')
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', head))
        elif line and not line.startswith('#') and attrs is not None:
            out.append((attrs, name.strip(), line))
            attrs = None
    return out


def norm(name):
    name = re.sub(r'[Ⓐ-ⓩ]', '', name)  # Free-TV's circled markers
    name = re.sub(r'\((\d+p|[^)]*geo[^)]*)\)|\[[^\]]*\]', '', name, flags=re.I)
    name = re.sub(r'\b(hd|fhd|uhd|sd|4k)\b', '', name, flags=re.I)
    return re.sub(r'[^0-9a-zà-ÿçğıöşü]+', '', name.lower().replace('+', 'plus'))  # AMC+ is not AMC


def clean_name(name):
    return re.sub(r'\s+', ' ', re.sub(r'[Ⓐ-ⓩ]|\[[^\]]*\]', '', name)).strip()


def slug(s):
    return re.sub(r'[^A-Za-z0-9]+', '_', s).strip('_')


def group_for(lang, categories):
    if lang != 'eng':
        return LANGS[lang]
    for g, cats in ENGLISH_GROUPS:
        if cats & set(categories):
            return g
    return 'English Entertainment'


def kind_for(categories):
    """A channel's category whatever its language ('Kids', 'News', ...): a programme without
    genres gets it, so Jellyfin's Movies, Sports, Kids and News rows have every such channel."""
    return group_for('eng', categories).removeprefix('English ')


def collect():
    """Candidate channels: key -> {name, logo, group, sources: [(url, quality, geo)]}."""
    get = lambda f: json.loads(fetch(API + f, timeout=60)[3])
    channels = {c['id']: c for c in get('channels.json')}
    langs = {}
    for f in get('feeds.json'):
        langs.setdefault(f['channel'], set()).update(f['languages'])
    logos = {}
    for l in get('logos.json'):
        if not l.get('feed'):
            logos.setdefault(l['channel'], l['url'])

    found = {}

    def add(key, name, logo, group, url, quality, geo, kind=None):
        ch = found.setdefault(key, {'name': name, 'logo': logo, 'group': group, 'kind': kind, 'sources': []})
        ch['logo'] = ch['logo'] or logo
        if not url.startswith(('http://', 'https://')):
            return
        if url not in [s[0] for s in ch['sources']]:
            ch['sources'].append((url, quality, geo))

    def from_iptv_org(cid):
        c = channels.get(cid)
        if not c or c['is_nsfw'] or c['closed'] or SKIP_CATEGORIES & set(c['categories']):
            return None
        ls = langs.get(cid, set())
        lang = next((l for l in ('swe', 'tur', 'eng') if l in ls), None)
        if lang == 'eng' and c['country'] not in ENGLISH_COUNTRIES:
            lang = None
        return c, lang

    for s in get('streams.json'):
        if s['user_agent'] or s['referrer'] or not s['channel']:
            continue  # needs custom headers most players can't send
        r = from_iptv_org(s['channel'])
        if not r or not r[1]:
            continue
        c, lang = r
        add(c['id'], c['name'], logos.get(c['id']), group_for(lang, c['categories']),
            s['url'], s['quality'] or '', 'Geo-blocked' in s['labels'], kind_for(c['categories']))

    for attrs, name, url in parse_m3u(fetch(FREETV, timeout=60)[3].decode('utf8')):
        lang = FREETV_GROUPS.get(attrs.get('group-title'))
        if not lang or 'youtube.com' in url or 'twitch.tv' in url:
            continue
        cid = attrs.get('tvg-id', '').split('@')[0]
        r = from_iptv_org(cid) if cid in channels else None
        if cid in channels and not r:
            continue  # known channel that we filter out (closed, shop, ...)
        key = cid if r else f'n:{norm(name)}'
        cats = r[0]['categories'] if r else []
        add(key, r[0]['name'] if r else clean_name(name), attrs.get('tvg-logo') or logos.get(cid),
            group_for(lang, cats), url, '', 'Ⓖ' in name, kind_for(cats))

    # Merge channels with the same name in the same group (iptv-org ids win over name keys).
    by_name = {}
    for k in sorted(found, key=lambda k: k.startswith('n:')):
        target = by_name.setdefault((norm(found[k]['name']), found[k]['group']), k)
        if target != k:
            for src in found.pop(k)['sources']:
                add(target, None, None, None, *src)
    return found


def check(url):
    """(ok, latency). ok: True alive, False dead, None blocked (403/451, maybe geo)."""
    t0 = time.monotonic()
    try:
        status, final, ctype, body = fetch(url, timeout=10, limit=65536)
        text = body.decode('utf8', 'ignore')
        if text.lstrip().startswith('#EXTM3U'):
            lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith('#')]
            if not lines:
                return False, 0
            nxt = urllib.parse.urljoin(final, lines[0])
            if '#EXT-X-STREAM-INF' in text:  # master playlist: open first variant
                _, final, _, body = fetch(nxt, timeout=10, limit=65536)
                lines = [l.strip() for l in body.decode('utf8', 'ignore').splitlines()
                         if l.strip() and not l.startswith('#')]
                if not lines:
                    return False, 0
                nxt = urllib.parse.urljoin(final, lines[0])
            fetch(nxt, timeout=10, limit=2048, headers={'Range': 'bytes=0-2047'})
        elif not (ctype.startswith(('video/', 'audio/', 'application/octet-stream'))
                  or body[:1] == b'\x47'):
            return False, 0
        return True, time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return (None if e.code in (403, 451) else False), 0
    except Exception:
        return False, 0


QUALITY = {'2160p': 4, '1080p': 3, '720p': 2, '576p': 1, '480p': 1}


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def xmltv_time(s):
    """Epoch seconds from an XMLTV time like '20261121203700 +0100' (no offset: UTC), else None."""
    m = re.fullmatch(r'\s*(\d{12}|\d{14})\s*([+-]\d{4})?\s*', s or '')
    try:
        return int(datetime.strptime(m[1].ljust(14, '0') + (m[2] or '+0000'), '%Y%m%d%H%M%S%z').timestamp())
    except (TypeError, ValueError):
        return None


def channel_keys(c):
    """What an XMLTV channel can match a channel by: its iptv-org id, Pluto/Roku-style ids in the
    URL it plays first (later sources can be other channels), and its name within its country (an iptv-org id ends in the country). A Swedish or
    Turkish channel only takes a Swedish or Turkish schedule."""
    keys = {('id', c['key'].lower())}
    keys |= {('id', h) for s in c['sources'][:1]
             for h in re.findall(r'(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f]{24})(?![0-9a-f])', s[4].lower())}
    country = None if c['key'].startswith('n:') else c['key'].rsplit('.', 1)[-1].lower()
    if local := {'Swedish': 'se', 'Turkish': 'tr'}.get(c['group']):
        country = local if country in (None, local) else None
    if country and len(norm(c['name'])) >= 3:
        keys.add(('name', country, norm(c['name'])))
    return keys


def xmltv_keys(xid, names, country=None):
    """The same keys for an XMLTV channel: its names in the country its id ends in (epgshare01:
    '[SVT1HD].SVT1.HD.se', 'CW.us2'), else in the feed's country."""
    if m := re.fullmatch(r'(?:\[[^\]]*\]\.)?(.+)\.([a-z]{2})\d*', xid, re.I):
        names, country = names + [m[1].replace('.', ' ')], m[2].lower()
    return {('id', xid.lower())} | {('name', country, norm(n)) for n in names if country and len(norm(n)) >= 3}


class Capped:
    """A file that refuses to read more than `left` bytes (a gzip bomb stops here)."""
    def __init__(self, f, left):
        self.f, self.left = f, left

    def read(self, n=-1):
        data = self.f.read(n)
        self.left -= len(data)
        if self.left < 0:
            raise ValueError('feed too large')
        return data


def epg_feeds():
    for url, country in EPG_FEEDS:
        try:
            blob = fetch(url, timeout=60, limit=MAX_FEED)[3]
        except Exception as e:
            print(f'guide {url}: {e}', flush=True)
            continue
        yield url, country, Capped(gzip.GzipFile(fileobj=io.BytesIO(blob)), MAX_XML)


def read_guides(feeds, channels, lo, hi):
    """{channel key: (feed, [(start, end, title, desc, genres)])}: each channel's best XMLTV match
    with programmes in [lo, hi). One with programmes beats one without, an id match beats a name
    match, then the one with most programmes wins."""
    lookup = {}
    for c in channels:
        for k in channel_keys(c):
            lookup.setdefault(k, []).append(c['key'])
    links, progs = {}, {}  # links: key -> {source: rank}; progs: source -> programmes
    clean = lambda s, cap: ' '.join((s or '').split())[:cap]
    for feed, country, f in feeds:
        try:
            events = ET.iterparse(f, events=('start', 'end'))
            _, root = next(events)
            for event, el in events:
                if event != 'end' or el.tag not in ('channel', 'programme'):
                    continue
                if el.tag == 'channel':
                    src = (feed, el.get('id', ''))
                    for k in xmltv_keys(src[1], [n.text or '' for n in el.iter('display-name')], country):
                        for key in lookup.get(k, ()):
                            rank = links.setdefault(key, {}).get(src, 1)
                            links[key][src] = min(rank, k[0] == 'name')
                            progs.setdefault(src, [])
                elif (src := (feed, el.get('channel', ''))) in progs:
                    s, e, title = xmltv_time(el.get('start')), xmltv_time(el.get('stop')), clean(el.findtext('title'), MAX_TITLE)
                    if s and e and title and 0 < e - s <= DAY and e > lo and s < hi:
                        genres = [clean(g.text, 40) for g in el.iter('category')]
                        progs[src].append((s, e, title, clean(el.findtext('desc'), MAX_DESC),
                                           list(dict.fromkeys(g for g in genres if g))[:3]))
                root.clear()
        except Exception as e:  # a broken feed (bad gzip, bad XML, too large) only loses itself
            print(f'guide {feed}: {e} (keeping what was read)', flush=True)
    found = {}
    for key, srcs in links.items():
        src = min(srcs, key=lambda s: (not progs[s], srcs[s], -len(progs[s])))
        if progs[src]:
            found[key] = (src[0], progs[src])
    return found


def placeholders(a, b):
    """Placeholder blocks over [a, b), cut at UTC-aligned BLOCK boundaries (no slivers)."""
    out = []
    while a < b:
        n = (a // BLOCK + 1) * BLOCK
        n = min(b, n + BLOCK if n - a < MIN_GAP else n)
        out.append((a, n, None, '', []))
        a = n
    return out


def schedule(progs, lo, hi):
    """A contiguous schedule over [lo, hi): real programmes, an overlap trimming the earlier one,
    and placeholders (title None) in the gaps."""
    real = []
    for p in sorted(progs):
        if real and p[0] < real[-1][1]:
            if p[0] <= real[-1][0]:
                continue
            real[-1] = (real[-1][0], p[0], *real[-1][2:])
        real.append(p)
    out, t = [], lo
    for p in real + [(hi, hi)]:
        if out and out[-1][2] and 0 < p[0] - t < MIN_GAP:
            out[-1] = (out[-1][0], p[0], *out[-1][2:])
        else:
            out += placeholders(t, p[0])
        if p[0] < hi:
            out.append(p)
            t = max(t, p[1])
    return out


def videos(sched, a, b, cid, name, kind=None):
    """The schedule's programmes overlapping [a, b) as Stremio videos; no genres means the channel's kind."""
    out = []
    for s, e, title, desc, genres in sched:
        if e > a and s < b:
            v = {'id': f'{cid}:{s}', 'title': title or f'{name} – Live', 'startTime': iso(s), 'endTime': iso(e)}
            genres = genres or ([kind] if kind else [])
            out.append({**v, **({'overview': desc} if desc else {}), **({'genres': genres} if genres else {})})
    return out


def guide_path(genre, date):
    """Where AIOStreams asks for a day: extras in its order (genre, date), the host decoding the URL."""
    return f'catalog/tv/channels/{f"genre={genre}&" if genre else ""}date={date}.json'


def build(out=Path('site')):
    found = collect()
    urls = {s[0] for ch in found.values() for s in ch['sources']}
    print(f'{len(found)} candidate channels, {len(urls)} streams; checking...', flush=True)
    with cf.ThreadPoolExecutor(64) as ex:
        results = dict(zip(urls, ex.map(check, urls)))

    channels = []
    for key, ch in found.items():
        good = []
        for url, quality, geo in ch['sources']:
            ok, lat = results[url]
            if ok or (ok is None and geo):  # keep geo-blocked ones the build server can't reach
                good.append((not url.startswith('https'), not ok, -QUALITY.get(quality, 0), lat, url, quality))
        if good:
            good.sort()
            channels.append({**ch, 'key': key, 'id': 'ftv_' + slug(key), 'sources': good[:MAX_SOURCES]})

    def order(c):
        names = [norm(n) for n in PRIORITY.get(c['group'], [])]
        p = names.index(norm(c['name'])) if norm(c['name']) in names else len(names)
        return GROUP_ORDER.index(c['group']), p, c['name'].lower()
    channels.sort(key=order)

    lo = int(time.time()) // DAY * DAY + GUIDE_DAYS[0] * DAY
    hi = lo + len(GUIDE_DAYS) * DAY
    t0 = time.monotonic()
    real = read_guides(epg_feeds(), channels, lo, hi)
    by_feed = {}
    for feed, _ in real.values():
        by_feed[feed] = by_feed.get(feed, 0) + 1
    print(f'guides read in {time.monotonic() - t0:.0f}s: {len(real)}/{len(channels)} channels with schedules '
          f'{by_feed}', flush=True)
    for c in channels:
        c['guide'] = schedule(real.get(c['key'], (None, []))[1], lo, hi)

    if out.exists():
        shutil.rmtree(out)
    groups = [g for g in GROUP_ORDER if any(c['group'] == g for c in channels)]
    write = lambda p, data: (out / p).parent.mkdir(parents=True, exist_ok=True) or \
        (out / p).write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), 'utf8')
    write('manifest.json', {
        'id': 'community.freetv.curated', 'version': '1.1.0', 'name': 'Free TV',
        'description': 'Free-to-air Swedish, Turkish and English channels from iptv-org and '
                       'Free-TV, health-checked and deduplicated daily.',
        'resources': ['catalog', 'meta', 'stream'], 'types': ['tv'], 'idPrefixes': ['ftv_'],
        'catalogs': [{'type': 'tv', 'id': 'channels', 'name': 'Free TV',
                      'extra': [{'name': 'genre', 'options': groups}, {'name': 'date'}]}],
        'behaviorHints': {'configurable': False, 'epgProvider': True}})

    def meta(c, n):
        return {'id': c['id'], 'type': 'tv', 'name': c['name'], 'logo': c['logo'],
                'poster': c['logo'], 'posterShape': 'square', 'genres': [c['group']],
                'channelNumber': n, 'behaviorHints': {'isLive': True}}

    metas = [meta(c, n) for n, c in enumerate(channels, 1)]
    write('catalog/tv/channels.json', {'metas': metas})
    for g in groups:
        write(f'catalog/tv/channels/genre={g}.json', {'metas': [m for m in metas if m['genres'] == [g]]})
    for day in range(lo, hi, DAY):  # the guide: one file per UTC day, and per group and day
        date = time.strftime('%Y-%m-%d', time.gmtime(day))
        dated = [{**m, 'videos': videos(c['guide'], day, day + DAY, c['id'], c['name'], c['kind'])} for m, c in zip(metas, channels)]
        write(guide_path(None, date), {'metasDetailed': dated})
        for g in groups:
            write(guide_path(g, date), {'metasDetailed': [m for m in dated if m['genres'] == [g]]})
    m3u = ['#EXTM3U']
    for m, c in zip(metas, channels):
        # defaultVideoId: Stremio plays the channel instead of listing its programmes
        hints = {**m['behaviorHints'], 'defaultVideoId': c['id']}
        write(f'meta/tv/{c["id"]}.json', {'meta': {**m, 'behaviorHints': hints, 'videos': videos(c['guide'], lo, hi, c['id'], c['name'], c['kind'])}})
        write(f'stream/tv/{c["id"]}.json', {'streams': [
            {'url': s[4], 'name': 'Free TV', 'title': f'Source {i}' + (f' · {s[5]}' if s[5] else ''),
             'behaviorHints': {'notWebReady': not s[4].startswith('https')}}
            for i, s in enumerate(c['sources'], 1)]})
        m3u += [f'#EXTINF:-1 tvg-chno="{m["channelNumber"]}" tvg-logo="{c["logo"] or ""}" '
                f'group-title="{c["group"]}",{c["name"]}', c['sources'][0][4]]
    (out / 'playlist.m3u').write_text('\n'.join(m3u) + '\n', 'utf8')
    stats = {g: sum(c['group'] == g for c in channels) for g in groups}
    guided = {g: sum(c['group'] == g and c['key'] in real for c in channels) for g in groups}
    (out / 'index.html').write_text(
        '<!doctype html><meta charset="utf-8"><title>Free TV</title><h1>Free TV</h1>'
        f'<p>{len(channels)} working channels, built {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}.</p>'
        '<p>Add <code>manifest.json</code> from this site to AIOStreams or Stremio. '
        'Plain playlist: <a href="playlist.m3u">playlist.m3u</a>.</p>'
        f'<p>Programme guide {time.strftime("%Y-%m-%d", time.gmtime(lo))} to '
        f'{time.strftime("%Y-%m-%d", time.gmtime(hi - DAY))}: {len(real)} channels with real schedules '
        '(Pluto TV, Samsung TV Plus, Plex and Roku from i.mjh.nz; epgshare01), "Live" blocks for the rest.</p><ul>'
        + ''.join(f'<li>{g}: {n} ({guided[g]} with schedules)</li>' for g, n in stats.items()) + '</ul>', 'utf8')
    alive = sum(1 for v in results.values() if v[0])
    print(f'{alive}/{len(urls)} streams alive; {len(channels)} channels written: {stats}')


def test():
    items = parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id="SVT1.se" group-title="Sweden",SVT1 HD Ⓖ\nhttp://a/b.m3u8\n')
    assert items == [({'tvg-id': 'SVT1.se', 'group-title': 'Sweden'}, 'SVT1 HD Ⓖ', 'http://a/b.m3u8')]
    assert norm('SVT1 HD Ⓖ') == norm('SVT 1') == 'svt1'
    assert clean_name('BBC One Ⓖ') == 'BBC One'
    assert group_for('eng', ['general', 'news']) == 'English News'
    assert group_for('swe', ['news']) == 'Swedish'
    assert kind_for(['news']) == 'News' and kind_for(['kids']) == 'Kids' and kind_for([]) == 'Entertainment'

    # XMLTV times: offsets are subtracted to UTC, 12-digit times and missing offsets are UTC.
    t0 = 1795305600  # 2026-11-22T00:00:00Z
    assert iso(t0) == '2026-11-22T00:00:00Z'
    assert xmltv_time('20261122000000 +0000') == xmltv_time('20261122000000') == t0
    assert xmltv_time('20261122010000 +0100') == t0 == xmltv_time('202611220000')
    assert xmltv_time('20261121190000 -0500') == t0
    assert xmltv_time('20261341000000 +0000') is None and xmltv_time('soon') is None and xmltv_time(None) is None

    # Gaps become UTC-aligned placeholder blocks; overlaps trim the earlier programme; tiny gaps
    # stretch it; a sliver before a block boundary joins the next block.
    H = 3600
    p = lambda s, e, t='A': (t0 + s, t0 + e, t, '', [])
    sched = schedule([p(H, 2 * H)], t0, t0 + 6 * H)
    assert [(s - t0, e - t0, t) for s, e, t, *_ in sched] == \
        [(0, H, None), (H, 2 * H, 'A'), (2 * H, 3 * H, None), (3 * H, 6 * H, None)]
    sched = schedule([p(0, 2 * H, 'A'), p(H, 2 * H + 100, 'B'), p(2 * H + 200, 3 * H - 10, 'C')], t0, t0 + 6 * H)
    assert [(s - t0, e - t0, t) for s, e, t, *_ in sched] == \
        [(0, H, 'A'), (H, 2 * H + 200, 'B'), (2 * H + 200, 3 * H - 10, 'C'), (3 * H - 10, 6 * H, None)]
    assert schedule([], t0, t0 + 6 * H) == [(t0, t0 + 3 * H, None, '', []), (t0 + 3 * H, t0 + 6 * H, None, '', [])]
    assert schedule([p(-H, H, 'A')], t0, t0 + 2 * H) == [p(-H, H, 'A'), (t0 + H, t0 + 2 * H, None, '', [])]
    # Days hold every programme that overlaps them, so one crossing midnight is in both.
    sched = schedule([p(-H, H, 'Late')], t0 - DAY, t0 + DAY)
    assert [v['title'] for v in videos(sched, t0, t0 + DAY, 'ftv_X', 'X')][:2] == ['Late', 'X – Live']
    assert videos(sched, t0, t0 + DAY, 'ftv_X', 'X', 'News')[1]['genres'] == ['News']
    assert videos(sched, t0 - DAY, t0, 'ftv_X', 'X')[-1] == \
        {'id': f'ftv_X:{t0 - H}', 'title': 'Late', 'startTime': '2026-11-21T23:00:00Z', 'endTime': '2026-11-22T01:00:00Z'}

    # XMLTV: match by id (Pluto ids in stream URLs) or by name within the channel's country.
    ch = {'key': 'SVT1.se', 'name': 'SVT1', 'group': 'Swedish', 'sources': [(0, 0, 0, 0, 'http://x/a.m3u8')]}
    pluto = {'key': 'PlutoTVAction.us', 'name': 'Pluto TV Action', 'group': 'English Movies',
             'sources': [(0, 0, 0, 0, 'https://jmp2.uk/plu-561d7d484dc7c8770484914a.m3u8')]}
    fta = {'key': 'n:trt1', 'name': 'TRT 1', 'group': 'Turkish', 'sources': []}
    assert ('name', 'se', 'svt1') in channel_keys(ch) and ('id', 'svt1.se') in channel_keys(ch)
    assert ('id', '561d7d484dc7c8770484914a') in channel_keys(pluto) and ('name', 'tr', 'trt1') in channel_keys(fta)
    assert xmltv_keys('[SVT1HD].SVT1.HD.se', []) >= {('name', 'se', 'svt1'), ('id', '[svt1hd].svt1.hd.se')}
    assert ('name', 'us', 'abcnews') in xmltv_keys('ABC.News.us2', ['ABC News'], 'uk')
    assert xmltv_keys('USBD27000', ['Bob Ross'], 'us') == {('id', 'usbd27000'), ('name', 'us', 'bobross')}
    assert xmltv_keys('5ba3fb9c4b078e0f37ad34e8', ['Pluto TV Action']) == {('id', '5ba3fb9c4b078e0f37ad34e8')}
    baby = {'key': 'BabyTV.uk', 'name': 'BabyTV', 'group': 'Turkish', 'sources': []}
    assert not any(k[0] == 'name' for k in channel_keys(baby))
    xml = f'''<?xml version="1.0"?><tv>
      <channel id="[SVT1HD].SVT1.HD.se"/><channel id="SVT1.uk"><display-name>SVT 1</display-name></channel>
      <channel id="561d7d484dc7c8770484914a"><display-name>Action</display-name></channel>
      <programme channel="[SVT1HD].SVT1.HD.se" start="20261122060000 +0100" stop="20261122070000 +0100">
        <title>Morgon</title><desc>{'x' * 3000}</desc><category>News</category><category>News</category></programme>
      <programme channel="[SVT1HD].SVT1.HD.se" start="20261122070000 +0000" stop="20261122060000 +0000"><title>Back</title></programme>
      <programme channel="[SVT1HD].SVT1.HD.se" start="bad" stop="20261122060000 +0000"><title>Bad</title></programme>
      <programme channel="[SVT1HD].SVT1.HD.se" start="20261122080000 +0000" stop="20261124080000 +0000"><title>Long</title></programme>
      <programme channel="SVT1.uk" start="20261122060000 +0000" stop="20261122070000 +0000"><title>UK</title></programme>
      <programme channel="561d7d484dc7c8770484914a" start="20261122000000 +0000" stop="20261122010000 +0000"><title>Film</title></programme>
    </tv>'''.encode()
    found = read_guides([('t', None, io.BytesIO(xml))], [ch, pluto, fta], t0, t0 + DAY)
    assert set(found) == {'SVT1.se', 'PlutoTVAction.us'}
    assert found['SVT1.se'][0] == 't'
    (s, e, title, desc, genres), = found['SVT1.se'][1]
    assert (s - t0, e - t0, title, len(desc), genres) == (5 * H, 6 * H, 'Morgon', MAX_DESC, ['News'])
    assert [x[2] for x in found['PlutoTVAction.us'][1]] == ['Film']
    # AMC+ is not AMC; only the first source's ids count; an empty id match yields to a name match;
    # a broken feed loses only itself.
    assert norm('AMC+') == 'amcplus' != norm('AMC HD')
    two = {**pluto, 'sources': [(0, 0, 0, 0, 'http://cdn/x.m3u8'), *pluto['sources']]}
    assert not any(k[0] == 'id' and len(k[1]) == 24 for k in channel_keys(two))
    xml2 = b'''<tv><channel id="561d7d484dc7c8770484914a"/><channel id="SVT1.se"/>
      <channel id="x.se"><display-name>SVT 1</display-name></channel>
      <programme channel="x.se" start="20261122000000" stop="20261122010000"><title>Named</title></programme></tv>'''
    found = read_guides([('bad', None, gzip.GzipFile(fileobj=io.BytesIO(gzip.compress(b"x")[:12] + b'x' * 50))),
                         ('t', None, io.BytesIO(xml2))], [ch], t0, t0 + DAY)
    assert [x[2] for x in found['SVT1.se'][1]] == ['Named']

    # Guide files sit where AIOStreams asks: genre then date, values percent-encoded, no skip.
    g, d = 'English Docs & Lifestyle', '2026-11-22'
    asked = f'catalog/tv/channels/genre={urllib.parse.quote(g, safe="")}&date={d}.json'
    assert urllib.parse.unquote(asked) == guide_path(g, d) == f'catalog/tv/channels/genre={g}&date={d}.json'
    assert guide_path(None, d) == f'catalog/tv/channels/date={d}.json'
    print('ok')


if __name__ == '__main__':
    test() if '--test' in sys.argv else build()
