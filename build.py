"""Build a static Stremio Live TV addon from iptv-org + Free-TV.

Merges both sources, keeps Swedish/Turkish/English channels, health-checks
every stream, dedupes channels and writes a static addon into ./site.
Stdlib only. Run: python build.py   (python build.py --test for the self-check)
"""
import concurrent.futures as cf
import json
import re
import shutil
import ssl
import sys
import time
import urllib.parse
import urllib.request
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
    return re.sub(r'[^0-9a-zà-ÿçğıöşü]+', '', name.lower())


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

    def add(key, name, logo, group, url, quality, geo):
        ch = found.setdefault(key, {'name': name, 'logo': logo, 'group': group, 'sources': []})
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
            s['url'], s['quality'] or '', 'Geo-blocked' in s['labels'])

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
            group_for(lang, cats), url, '', 'Ⓖ' in name)

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
            channels.append({**ch, 'id': 'ftv_' + slug(key), 'sources': good[:MAX_SOURCES]})

    def order(c):
        names = [norm(n) for n in PRIORITY.get(c['group'], [])]
        p = names.index(norm(c['name'])) if norm(c['name']) in names else len(names)
        return GROUP_ORDER.index(c['group']), p, c['name'].lower()
    channels.sort(key=order)

    if out.exists():
        shutil.rmtree(out)
    groups = [g for g in GROUP_ORDER if any(c['group'] == g for c in channels)]
    write = lambda p, data: (out / p).parent.mkdir(parents=True, exist_ok=True) or \
        (out / p).write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), 'utf8')
    write('manifest.json', {
        'id': 'community.freetv.curated', 'version': '1.0.0', 'name': 'Free TV',
        'description': 'Free-to-air Swedish, Turkish and English channels from iptv-org and '
                       'Free-TV, health-checked and deduplicated daily.',
        'resources': ['catalog', 'meta', 'stream'], 'types': ['tv'], 'idPrefixes': ['ftv_'],
        'catalogs': [{'type': 'tv', 'id': 'channels', 'name': 'Free TV',
                      'extra': [{'name': 'genre', 'options': groups}]}],
        'behaviorHints': {'configurable': False}})

    def meta(c, n):
        return {'id': c['id'], 'type': 'tv', 'name': c['name'], 'logo': c['logo'],
                'poster': c['logo'], 'posterShape': 'square', 'genres': [c['group']],
                'channelNumber': n, 'behaviorHints': {'isLive': True}}

    metas = [meta(c, n) for n, c in enumerate(channels, 1)]
    write('catalog/tv/channels.json', {'metas': metas})
    for g in groups:
        write(f'catalog/tv/channels/genre={g}.json', {'metas': [m for m in metas if m['genres'] == [g]]})
    m3u = ['#EXTM3U']
    for m, c in zip(metas, channels):
        write(f'meta/tv/{c["id"]}.json', {'meta': m})
        write(f'stream/tv/{c["id"]}.json', {'streams': [
            {'url': s[4], 'name': 'Free TV', 'title': f'Source {i}' + (f' · {s[5]}' if s[5] else ''),
             'behaviorHints': {'notWebReady': not s[4].startswith('https')}}
            for i, s in enumerate(c['sources'], 1)]})
        m3u += [f'#EXTINF:-1 tvg-chno="{m["channelNumber"]}" tvg-logo="{c["logo"] or ""}" '
                f'group-title="{c["group"]}",{c["name"]}', c['sources'][0][4]]
    (out / 'playlist.m3u').write_text('\n'.join(m3u) + '\n', 'utf8')
    stats = {g: sum(c['group'] == g for c in channels) for g in groups}
    (out / 'index.html').write_text(
        '<!doctype html><meta charset="utf-8"><title>Free TV</title><h1>Free TV</h1>'
        f'<p>{len(channels)} working channels, built {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}.</p>'
        '<p>Add <code>manifest.json</code> from this site to AIOStreams or Stremio. '
        'Plain playlist: <a href="playlist.m3u">playlist.m3u</a>.</p><ul>'
        + ''.join(f'<li>{g}: {n}</li>' for g, n in stats.items()) + '</ul>', 'utf8')
    alive = sum(1 for v in results.values() if v[0])
    print(f'{alive}/{len(urls)} streams alive; {len(channels)} channels written: {stats}')


def test():
    items = parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id="SVT1.se" group-title="Sweden",SVT1 HD Ⓖ\nhttp://a/b.m3u8\n')
    assert items == [({'tvg-id': 'SVT1.se', 'group-title': 'Sweden'}, 'SVT1 HD Ⓖ', 'http://a/b.m3u8')]
    assert norm('SVT1 HD Ⓖ') == norm('SVT 1') == 'svt1'
    assert clean_name('BBC One Ⓖ') == 'BBC One'
    assert group_for('eng', ['general', 'news']) == 'English News'
    assert group_for('swe', ['news']) == 'Swedish'
    print('ok')


if __name__ == '__main__':
    test() if '--test' in sys.argv else build()
