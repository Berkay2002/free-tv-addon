# Free TV

A static Stremio/AIOStreams Live TV addon with free-to-air Swedish, Turkish and English channels.

Channels come from [iptv-org](https://github.com/iptv-org/iptv) and [Free-TV/IPTV](https://github.com/Free-TV/IPTV).
`build.py` merges both, drops NSFW, closed, shopping and religious channels, checks every stream, removes dead ones,
merges duplicates and keeps up to three working sources per channel. A GitHub Action rebuilds it daily and pushes it to the `site` branch.

- Addon: `https://raw.githubusercontent.com/Berkay2002/free-tv-addon/site/manifest.json`
- Plain playlist: `https://raw.githubusercontent.com/Berkay2002/free-tv-addon/site/playlist.m3u`

## Programme guide

Every channel has programmes from yesterday through six days ahead (UTC days), so Jellyfin clients such as
Streamyfin, which play Live TV from the guide, always have something to tap. Real schedules come from free XMLTV
feeds: [i.mjh.nz](https://i.mjh.nz) for Pluto TV (matched by the channel id in the stream URL), Samsung TV Plus,
Plex and Roku, and [epgshare01](https://epgshare01.online/epgshare01/) for Sweden, Turkey, the UK, US, Canada,
Australia, Ireland and New Zealand (matched by channel name within the country). Times without a schedule are
filled with three-hour "<channel> – Live" blocks. The catalog declares the `date` extra and the guide is written as
`catalog/tv/channels/date=YYYY-MM-DD.json` and `catalog/tv/channels/genre=<group>&date=YYYY-MM-DD.json`, the paths
AIOStreams asks for; each channel's meta carries the whole week too.

Change the languages, groups, channel order or guide feeds at the top of `build.py`. `python build.py --test` runs the self-check.
