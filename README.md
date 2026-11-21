# Free TV

A static Stremio/AIOStreams Live TV addon with free-to-air Swedish, Turkish and English channels.

Channels come from [iptv-org](https://github.com/iptv-org/iptv) and [Free-TV/IPTV](https://github.com/Free-TV/IPTV).
`build.py` merges both, drops NSFW, closed, shopping and religious channels, checks every stream, removes dead ones,
merges duplicates and keeps up to three working sources per channel. A GitHub Action rebuilds it daily and publishes it
to GitHub Pages.

- Addon: `https://berkay2002.github.io/free-tv-addon/manifest.json`
- Plain playlist: `https://berkay2002.github.io/free-tv-addon/playlist.m3u`

Change the languages, groups or channel order at the top of `build.py`. `python build.py --test` runs the self-check.
